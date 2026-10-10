"""Safety policy: allowlist validation, threshold evaluation and the
persisted safety state (single-experiment lock, BLOCKED flag, kill request,
active-fault marker used for crash recovery).

Nothing in here depends on the dashboard or the API layer.
"""
from __future__ import annotations

import math
import os
import threading
import time
from dataclasses import dataclass
from typing import Iterable, Optional

from . import config
from .models import ExperimentConfig, Thresholds
from .storage import Store

# Probes that represent user-facing application behaviour. Their failures
# count toward the abort thresholds.
CRITICAL_PROBES = ("web_root", "api_documents", "api_status", "doc_upload")


def validate_config(cfg: ExperimentConfig, limits: config.SafetyLimits = config.LIMITS) -> list[str]:
    """Policy checks on top of the structural pydantic validation.
    Returns a list of violations (empty == allowed)."""
    errors: list[str] = []
    allowed = limits.allowlist.get(cfg.target)
    if allowed is None:
        errors.append(f"target '{cfg.target}' is not an approved sandbox resource "
                      f"(allowed: {sorted(limits.allowlist)})")
    elif cfg.fault_type not in allowed:
        errors.append(f"fault '{cfg.fault_type}' is not permitted on '{cfg.target}'")
    if not limits.min_duration_s <= cfg.duration_s <= limits.max_duration_s:
        errors.append(f"duration {cfg.duration_s}s outside [{limits.min_duration_s}, {limits.max_duration_s}]s")
    if cfg.fault_type == config.FAULT_LATENCY:
        if cfg.latency_ms is None:
            errors.append("latency_ms is required for network_latency")
        elif not limits.min_latency_ms <= cfg.latency_ms <= limits.max_latency_ms:
            errors.append(f"latency {cfg.latency_ms}ms outside [{limits.min_latency_ms}, {limits.max_latency_ms}]ms")
    elif cfg.latency_ms is not None:
        errors.append("latency_ms is only valid for network_latency")
    t = cfg.thresholds
    if not limits.min_latency_threshold_ms <= t.max_p95_latency_ms <= limits.max_latency_threshold_ms:
        errors.append(f"latency threshold {t.max_p95_latency_ms}ms outside "
                      f"[{limits.min_latency_threshold_ms}, {limits.max_latency_threshold_ms}]ms")
    if not limits.min_window_s <= t.window_s <= limits.max_window_s:
        errors.append(f"threshold window {t.window_s}s outside [{limits.min_window_s}, {limits.max_window_s}]s")
    if cfg.recovery_timeout_s > limits.max_recovery_timeout_s:
        errors.append(f"recovery timeout {cfg.recovery_timeout_s}s exceeds {limits.max_recovery_timeout_s}s")
    return errors


def percentile(values: list[float], pct: float) -> Optional[float]:
    if not values:
        return None
    s = sorted(values)
    k = max(0, min(len(s) - 1, math.ceil(pct / 100 * len(s)) - 1))
    return s[k]


@dataclass
class Breach:
    metric: str
    value: float
    limit: float
    samples: int

    def describe(self) -> str:
        if self.metric == "error_rate":
            return f"error rate {self.value:.0%} > limit {self.limit:.0%} over {self.samples} probes"
        return f"p95 latency {self.value:.0f}ms > limit {self.limit:.0f}ms over {self.samples} probes"

    def to_dict(self) -> dict:
        return {"metric": self.metric, "value": self.value, "limit": self.limit,
                "samples": self.samples, "message": self.describe()}


def evaluate_thresholds(samples: Iterable[dict], thresholds: Thresholds, now: float) -> list[Breach]:
    """Pure function: rolling-window error-rate and p95 latency check.
    Failed probes count with their (timeout) latency, so hangs show up in
    both metrics."""
    window = [s for s in samples
              if s["probe"] in CRITICAL_PROBES and s["ts"] >= now - thresholds.window_s]
    if len(window) < thresholds.min_samples:
        return []
    breaches = []
    failures = sum(1 for s in window if not s["ok"])
    err = failures / len(window)
    if err > thresholds.max_error_rate:
        breaches.append(Breach("error_rate", err, thresholds.max_error_rate, len(window)))
    p95 = percentile([s["latency_ms"] or 0.0 for s in window], 95)
    if p95 is not None and p95 > thresholds.max_p95_latency_ms:
        breaches.append(Breach("p95_latency_ms", p95, thresholds.max_p95_latency_ms, len(window)))
    return breaches


class SafetyState:
    """Persisted safety flags + an in-process lock preventing overlap."""

    ACTIVE = "active_fault"   # spec of the currently injected fault
    BLOCKED = "blocked"       # {reason, run_id, ts} when cleanup/recovery unverified
    KILL = "kill_requested"   # {reason, ts} set by any process (API, CLI)
    RUNNING = "running_run"   # run id currently owning the lock (informational)

    LOCK_STALE_S = 60.0  # a holder that stops heart-beating for this long is dead

    def __init__(self, store: Store):
        self.store = store
        self._lock = threading.Lock()
        self._owner: Optional[dict] = None

    # single-run lock (in-process + cross-process via SQLite) ------------------
    def try_acquire(self, run_id: str) -> bool:
        if not self._lock.acquire(blocking=False):
            return False
        owner = {"run_id": run_id, "pid": os.getpid()}
        if not self.store.kv_claim(self.RUNNING, owner, self.LOCK_STALE_S):
            self._lock.release()
            return False
        self._owner = owner
        return True

    def heartbeat(self) -> None:
        if self._owner:
            self.store.kv_touch(self.RUNNING)

    def release(self) -> None:
        if self._owner and self.store.kv_get(self.RUNNING) == self._owner:
            self.store.kv_delete(self.RUNNING)
        self._owner = None
        if self._lock.locked():
            self._lock.release()

    def busy(self) -> bool:
        if self._lock.locked():
            return True
        age = self.store.kv_age(self.RUNNING)
        return age is not None and age < self.LOCK_STALE_S

    def holder(self) -> Optional[dict]:
        return self.store.kv_get(self.RUNNING) if self.busy() else None

    def clear_stale_lock(self) -> None:
        age = self.store.kv_age(self.RUNNING)
        if age is not None and age >= self.LOCK_STALE_S and not self._lock.locked():
            self.store.kv_delete(self.RUNNING)

    # blocked -------------------------------------------------------------------
    def blocked(self) -> Optional[dict]:
        return self.store.kv_get(self.BLOCKED)

    def block(self, reason: str, run_id: str | None) -> None:
        self.store.kv_set(self.BLOCKED, {"reason": reason, "run_id": run_id, "ts": time.time()})
        self.store.event("blocked", f"Experiments blocked: {reason}", run_id, level="error")

    def unblock(self, note: str) -> None:
        self.store.kv_delete(self.BLOCKED)
        self.store.event("unblocked", note, level="warning")

    # kill switch -------------------------------------------------------------------
    def request_kill(self, reason: str) -> None:
        self.store.kv_set(self.KILL, {"reason": reason, "ts": time.time()})

    def kill_requested(self) -> Optional[dict]:
        return self.store.kv_get(self.KILL)

    def clear_kill(self) -> None:
        self.store.kv_delete(self.KILL)

    # active fault marker (crash recovery) -----------------------------------------
    def mark_active(self, spec: dict) -> None:
        self.store.kv_set(self.ACTIVE, spec)

    def active(self) -> Optional[dict]:
        return self.store.kv_get(self.ACTIVE)

    def clear_active(self) -> None:
        self.store.kv_delete(self.ACTIVE)
