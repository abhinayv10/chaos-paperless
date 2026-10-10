"""Repeatable synthetic workload + container health sampling.

Workload profile v1 (fixed, so runs are comparable):
  web_root        GET /                       every 2s
  api_documents   GET /api/documents/          every 2s   (DB read path)
  api_status      GET /api/status/             every 4s   (Paperless' own DB/Redis check)
  doc_upload      POST post_document (.txt)   every 8s   (Redis/Celery enqueue path)
  doc_processing  upload -> task SUCCESS      tracked per upload (worker + DB write path)
Every probe has a 5s client timeout. Each probe stream runs on its own
thread so a hanging call never stalls the others.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

from .models import ProbeResult
from .storage import Store

WORKLOAD_VERSION = "v1"
INTERVALS = {"web_root": 2.0, "api_documents": 2.0, "api_status": 4.0, "doc_upload": 8.0}
PROCESSING_TIMEOUT_S = 60.0
HEALTH_INTERVAL_S = 2.0


@dataclass
class Pending:
    task_id: str
    submitted: float
    phase: str


class Workload:
    def __init__(self, client, store: Store, run_id: str, phase_fn: Callable[[], str],
                 docker=None, intervals: dict[str, float] | None = None,
                 health_interval: float = HEALTH_INTERVAL_S,
                 processing_timeout: float = PROCESSING_TIMEOUT_S):
        self.client = client
        self.store = store
        self.run_id = run_id
        self.phase_fn = phase_fn
        self.docker = docker
        self.intervals = intervals or INTERVALS
        self.health_interval = health_interval
        self.processing_timeout = processing_timeout
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._pending: list[Pending] = []
        self._plock = threading.Lock()

    # ------------------------------------------------------------------------------
    def start(self) -> None:
        probes: dict[str, Callable[[], None]] = {
            "web_root": lambda: self._record(self.client.probe_web_root()),
            "api_documents": lambda: self._record(self.client.probe_documents()),
            "api_status": lambda: self._record(self.client.probe_status()),
            "doc_upload": self._upload,
        }
        for name, fn in probes.items():
            if name in self.intervals:
                self._spawn(f"probe-{name}", self._loop, fn, self.intervals[name])
        if "doc_upload" in self.intervals:
            self._spawn("poll-tasks", self._loop, self._poll_tasks, 1.0)
        if self.docker is not None:
            self._spawn("health", self._loop, self._sample_health, self.health_interval)

    def stop(self, drain_s: float = 0.0) -> None:
        """Stop issuing new probes; optionally keep resolving in-flight
        document processing for up to drain_s seconds."""
        deadline = time.time() + drain_s
        while drain_s and time.time() < deadline and self.pending_count():
            time.sleep(0.5)
        self._stop.set()
        for t in self._threads:
            t.join(timeout=10)
        with self._plock:
            leftovers, self._pending = self._pending, []
        for p in leftovers:
            self.store.add_sample(self.run_id, p.phase, ProbeResult(
                "doc_processing", False, (time.time() - p.submitted) * 1000, None,
                f"task {p.task_id} unresolved when workload stopped", p.submitted))

    def pending_count(self) -> int:
        with self._plock:
            return len(self._pending)

    # ------------------------------------------------------------------------------
    def _spawn(self, name: str, target, *args) -> None:
        t = threading.Thread(target=target, args=args, name=f"wl-{name}", daemon=True)
        self._threads.append(t)
        t.start()

    def _loop(self, fn: Callable[[], None], interval: float) -> None:
        while not self._stop.is_set():
            started = time.time()
            try:
                fn()
            except Exception as e:  # noqa: BLE001 - never let a sampler die silently
                self.store.event("workload_error", f"{type(e).__name__}: {e}", self.run_id, "warning")
            self._stop.wait(max(0.0, interval - (time.time() - started)))

    def _record(self, r: ProbeResult, phase: Optional[str] = None) -> None:
        self.store.add_sample(self.run_id, phase or self.phase_fn(), r)

    def _upload(self) -> None:
        phase = self.phase_fn()
        res, task_id = self.client.upload_document()
        self._record(res, phase)
        if task_id:
            with self._plock:
                self._pending.append(Pending(task_id, res.ts, phase))

    def _poll_tasks(self) -> None:
        with self._plock:
            pending = list(self._pending)
        for p in pending:
            age = time.time() - p.submitted
            try:
                task = self.client.task_status(p.task_id)
            except Exception as e:  # noqa: BLE001
                task, err = None, str(e)
            else:
                err = ""
            status = (task or {}).get("status")
            if status in ("success", "failure", "revoked") or age > self.processing_timeout:
                ok = status == "success"
                done = time.time()
                if status == "success" and task.get("duration_seconds") is not None:
                    detail = (f"task {p.task_id} success; wait={task.get('wait_time_seconds')}s "
                              f"run={task.get('duration_seconds')}s")
                elif status in ("failure", "revoked"):
                    detail = f"task {p.task_id} {status}: {str(task.get('result_data'))[:200]}"
                else:
                    detail = f"task {p.task_id} not finished after {age:.0f}s {err}".strip()
                self._record(ProbeResult("doc_processing", ok, (done - p.submitted) * 1000, None,
                                         detail, p.submitted), p.phase)
                with self._plock:
                    self._pending = [x for x in self._pending if x.task_id != p.task_id]

    def _sample_health(self) -> None:
        phase = self.phase_fn()
        now = time.time()
        for c in self.docker.project_containers():
            self.store.add_health(self.run_id, phase, c.service or c.name,
                                  "paused" if c.paused else c.state, c.health, now)
