"""Experiment orchestration.

Lifecycle of one experiment (one at a time):
  1. validate config against the safety policy           -> REJECTED on violation
  2. preflight: docker, sandbox health, target, app probes
  3. BASELINE: run the workload immediately before injection
  4. INJECTING/ACTIVE: apply fault, start independent watchdog, evaluate
     thresholds every tick; break on breach / kill / planned end
  5. ROLLING_BACK (finally): remove fault, verify removal, sweep if needed
  6. VERIFYING_RECOVERY: target healthy + N consecutive good app probes
  7. OBSERVING: post-recovery observation, drain document processing
  8. persist summary, impact, logs, outcome; BLOCK further experiments if
     removal or recovery could not be verified
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Optional

from . import config
from .docker_ops import Docker, DockerError, NotInSandbox
from .faults import build_fault, emergency_rollback
from .metrics import compare, summarize_health, summarize_samples
from .models import Check, ExperimentConfig, RunState, TERMINAL_STATES
from .safety import CRITICAL_PROBES, SafetyState, evaluate_thresholds, validate_config
from .storage import Store
from .workload import Workload

LOG_KEYWORDS = ("error", "exception", "traceback", "timeout", "timed out", "refused",
                "warning", "could not", "failed", "operationalerror", "connection")


class ExperimentRejected(Exception):
    def __init__(self, errors: list[str], checks: list[Check] | None = None, status: int = 400):
        super().__init__("; ".join(errors))
        self.errors = errors
        self.checks = checks or []
        self.status = status


def _run_id(prefix: str) -> str:
    return f"{prefix}-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{os.urandom(2).hex()}"


class Engine:
    def __init__(self, store: Store, docker: Docker, client_factory: Callable[[], object],
                 timings: config.Timings = config.TIMINGS, limits: config.SafetyLimits = config.LIMITS,
                 workload_kwargs: dict | None = None, spawn_watchdog: bool = True):
        self.store = store
        self.docker = docker
        self.client_factory = client_factory
        self.timings = timings
        self.limits = limits
        self.workload_kwargs = workload_kwargs or {}
        self.spawn_watchdog = spawn_watchdog
        self.safety = SafetyState(store)
        self._kill = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._live: dict = {}
        self._live_lock = threading.Lock()
        # self-healing mode: called with only the run id when an agent-mode
        # experiment starts; the agent reports its fix via agent_remediated()
        self.on_start: Optional[Callable[[str], None]] = None
        self._agent_fix = threading.Event()
        self._agent_note = ""

    # ------------------------------------------------------------------ live state
    def _set_live(self, **kw) -> None:
        with self._live_lock:
            self._live.update(kw)
            self._live["updated"] = time.time()
        if "state" in kw and self._live.get("run_id"):
            self.store.update_run(self._live["run_id"], state=kw["state"])

    def live(self) -> dict:
        with self._live_lock:
            return dict(self._live)

    def busy(self) -> bool:
        return self.safety.busy()

    def running_here(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------------ environment
    def verify_environment(self, client=None, require_app: bool = True) -> list[Check]:
        checks: list[Check] = []
        ok, ver = self.docker.available()
        checks.append(Check("docker engine reachable", ok, ver))
        if not ok:
            return checks
        try:
            containers = self.docker.project_containers()
        except DockerError as e:
            return checks + [Check("sandbox containers listed", False, str(e))]
        services = {c.service: c for c in containers}
        for svc in ("webserver", "db", "broker"):
            c = services.get(svc)
            if c is None:
                checks.append(Check(f"{svc} container present", False, "missing"))
                continue
            checks.append(Check(f"{svc} running, not paused, healthy", c.ok,
                                f"state={c.state} health={c.health} paused={c.paused}"))
            if not c.sandbox:
                checks.append(Check(f"{svc} carries sandbox label", False, "label chaos.sandbox missing"))
        try:
            sidecars = self.docker.netem_sidecars()
            checks.append(Check("no leftover fault sidecars", not sidecars, f"{len(sidecars)} found"))
        except DockerError as e:
            checks.append(Check("no leftover fault sidecars", False, str(e)))
        if require_app:
            client = client or self.client_factory()
            for probe in (client.probe_status(), client.probe_documents()):
                checks.append(Check(f"app probe {probe.probe}", probe.ok,
                                    f"{probe.detail} ({probe.latency_ms:.0f}ms)"))
        return checks

    def preflight(self, cfg: ExperimentConfig) -> tuple[list[str], list[Check], Optional[dict]]:
        errors = validate_config(cfg, self.limits)
        checks: list[Check] = [Check("config within safety policy", not errors, "; ".join(errors) or "ok")]
        blocked = self.safety.blocked()
        checks.append(Check("experiments not blocked", not blocked, (blocked or {}).get("reason", "ok")))
        checks.append(Check("no experiment running", not self.busy(), "busy" if self.busy() else "idle"))
        if errors:
            return errors, checks, None
        spec = None
        try:
            env_checks = self.verify_environment()
            checks += env_checks
            target = self.docker.resolve_service(cfg.target)
            checks.append(Check("target resolves to approved sandbox container", True,
                                f"{target.name} ({target.id[:12]})"))
            spec = {
                "fault_type": cfg.fault_type, "service": cfg.target, "container_id": target.id,
                "run_id": "preflight", "duration_s": cfg.duration_s, "latency_ms": cfg.latency_ms,
                "loss_pct": cfg.loss_pct, "cpu_limit": cfg.cpu_limit, "memory_mb": cfg.memory_mb,
            }
            checks += build_fault(self.docker, spec).preflight()
        except NotInSandbox as e:
            checks.append(Check("target resolves to approved sandbox container", False, str(e)))
        except DockerError as e:
            checks.append(Check("docker operations", False, str(e)))
        failed = [c for c in checks if not c.ok]
        return [f"preflight failed: {c.name} ({c.detail})" for c in failed], checks, spec

    # ------------------------------------------------------------------ experiments
    def start(self, cfg: ExperimentConfig) -> str:
        run_id = _run_id("exp")
        errors, checks, spec = self.preflight(cfg)
        if errors:
            self.store.create_run(run_id, "experiment", RunState.REJECTED.value, cfg.model_dump())
            self.store.update_run(run_id, ended=time.time(), outcome={
                "result": "rejected", "errors": errors, "preflight": [c.to_dict() for c in checks]})
            self.store.event("rejected", "; ".join(errors), run_id, "warning")
            status = 423 if self.safety.blocked() else 409 if self.busy() else 400
            raise ExperimentRejected(errors, checks, status)
        if not self.safety.try_acquire(run_id):
            raise ExperimentRejected(["another experiment or baseline is running"], checks, 409)
        try:
            self.safety.clear_kill()
            self._kill.clear()
            spec["run_id"] = run_id
            self.store.create_run(run_id, "experiment", RunState.PREFLIGHT.value, cfg.model_dump())
            self.store.update_run(run_id, outcome={"preflight": [c.to_dict() for c in checks]})
            self.store.event("preflight_passed", f"{len(checks)} checks passed", run_id)
            with self._live_lock:
                self._live = {"run_id": run_id, "kind": "experiment", "config": cfg.model_dump(),
                              "phase": "preflight", "breaches": [], "message": "preflight passed",
                              "preflight": [c.to_dict() for c in checks], "started": time.time()}
            self._set_live(state=RunState.PREFLIGHT.value)
            self._agent_fix.clear()
            self._agent_note = ""
            self._thread = threading.Thread(target=self._execute, args=(run_id, cfg, spec, checks),
                                            name=f"experiment-{run_id}", daemon=True)
            self._thread.start()
        except Exception:
            self.safety.release()
            raise
        if cfg.remediation == "agent" and self.on_start:
            try:
                self.on_start(run_id)
            except Exception as e:  # noqa: BLE001 - no agent just means the engine's deadline applies
                self.store.event("agent_error", f"could not attach agent: {type(e).__name__}: {e}", run_id, "error")
        return run_id

    def agent_remediated(self, run_id: str, note: str) -> dict:
        """The self-healing agent ran a runbook. The engine checks the real
        fault state on its next tick; a wrong fix leaves the fault in place."""
        live = self.live()
        if live.get("run_id") != run_id or live.get("state") not in (RunState.ACTIVE.value, RunState.INJECTING.value):
            return {"accepted": False, "reason": f"run is not in its fault window (state {live.get('state')})"}
        self._agent_note = note
        self._agent_fix.set()
        return {"accepted": True}

    def wait(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)

    def _killed(self) -> Optional[str]:
        self.safety.heartbeat()
        if self._kill.is_set():
            return "manual kill switch"
        k = self.safety.kill_requested()
        return f"kill switch: {k.get('reason', '')}" if k else None

    def _execute(self, run_id: str, cfg: ExperimentConfig, spec: dict, checks: list[Check]) -> None:
        t = self.timings
        phase = {"v": "baseline"}
        client = self.client_factory()
        wl = Workload(client, self.store, run_id, lambda: phase["v"], docker=self.docker,
                      **self.workload_kwargs)
        fault = build_fault(self.docker, spec)
        outcome: dict = {"preflight": [c.to_dict() for c in checks], "hypothesis": cfg.hypothesis}
        abort_reason: Optional[str] = None
        breaches: list[dict] = []
        injected = False
        fault_start = fault_end = None
        removal_ok = recovery_ok = None
        recovery_time = None
        wl_stopped = False
        agent_mode = cfg.remediation == "agent"
        remediated_by: Optional[str] = None
        observed_breaches: list[dict] = []
        try:
            # ---- 3. baseline immediately before injection
            self._set_live(state=RunState.BASELINE.value, phase="baseline",
                           message=f"collecting {t.pre_baseline_s:.0f}s pre-injection baseline")
            wl.start()
            end = time.time() + t.pre_baseline_s
            while time.time() < end and not self._killed():
                time.sleep(min(t.monitor_tick_s, 0.5))
            if self._killed():
                abort_reason = self._killed() + " (before injection; no fault applied)"
                raise _Abort()
            base = summarize_samples(self.store.samples(run_id)).get("baseline", {}).get("critical", {})
            if not base.get("n"):
                abort_reason = "no baseline samples collected; refusing to inject"
                raise _Abort()

            # ---- 4. inject (marker first, so a crash mid-injection is recoverable)
            self._set_live(state=RunState.INJECTING.value, message=f"injecting {cfg.fault_type} on {cfg.target}")
            self.safety.mark_active({**spec, "planned_end": time.time() + cfg.duration_s})
            try:
                # the fault starts when the injection starts: a docker stop takes
                # seconds, and requests in that window already see the failure
                inject_t = time.time()
                phase["v"] = "fault"
                fault.inject()
                injected = True
                fault_start = inject_t
                self.store.update_run(run_id, fault_start=fault_start)
                self.store.event("fault_injected", f"{cfg.fault_type} applied to {cfg.target} "
                                 f"for up to {cfg.duration_s}s", run_id, data=spec)
                grace = self.limits.watchdog_grace_s + (60 if cfg.fault_type == config.FAULT_MEMORY else 0)
                self._start_watchdog(spec, fault_start + cfg.duration_s + grace)
                planned_end = fault_start + cfg.duration_s
                self._set_live(state=RunState.ACTIVE.value, phase="fault", fault_start=fault_start,
                               planned_end=planned_end,
                               message="fault active; self-healing agent must detect and fix it" if agent_mode
                               else "fault active; monitoring thresholds")
                while time.time() < planned_end:
                    k = self._killed()
                    if k:
                        abort_reason = k
                        self.store.event("manual_abort", k, run_id, "warning")
                        break
                    if agent_mode and self._agent_fix.is_set():
                        self._agent_fix.clear()
                        try:
                            gone, detail = fault.verify_removed()
                        except Exception as e:  # noqa: BLE001
                            gone, detail = False, f"{type(e).__name__}: {e}"
                        if gone:
                            remediated_by = "agent"
                            self.store.event("agent_remediated", f"fault gone after the agent's runbook "
                                             f"({self._agent_note}): {detail}", run_id)
                            break
                        self.store.event("agent_fix_ineffective", f"{self._agent_note}: the injected fault is "
                                         f"still present ({detail})", run_id, "warning")
                    now = time.time()
                    window = [s for s in self.store.samples(run_id, since=now - cfg.thresholds.window_s - 1)
                              if s["phase"] == "fault"]
                    found = evaluate_thresholds(window, cfg.thresholds, now)
                    if found and agent_mode:
                        if not observed_breaches:
                            observed_breaches = [b.to_dict() for b in found]
                            self.store.event("threshold_breach_observed", "; ".join(b.describe() for b in found)
                                             + " (self-healing mode: no automatic abort; the fault ends when the "
                                             "agent fixes it or at the planned end)", run_id, "warning",
                                             data=observed_breaches)
                    elif found:
                        breaches = [b.to_dict() for b in found]
                        abort_reason = "automatic abort: " + "; ".join(b.describe() for b in found)
                        self.store.event("threshold_breach", abort_reason, run_id, "error", data=breaches)
                        self._set_live(breaches=breaches)
                        break
                    time.sleep(t.monitor_tick_s)
            except Exception as e:  # noqa: BLE001
                abort_reason = abort_reason or f"error during fault window: {type(e).__name__}: {e}"
                self.store.event("fault_error", abort_reason, run_id, "error")
            finally:
                # ---- 5. rollback: always runs, idempotent
                self._set_live(state=RunState.ROLLING_BACK.value, phase="recovery",
                               message=f"removing fault ({abort_reason or 'planned end'})")
                phase["v"] = "recovery"
                removal_ok, removal_notes = self._remove(fault, spec, run_id)
                fault_end = time.time()
                self.store.update_run(run_id, fault_end=fault_end)
                outcome["fault_removal"] = {"verified": removal_ok, "notes": removal_notes,
                                            "at": fault_end}
                if agent_mode:
                    outcome["remediation"] = {
                        "mode": "agent",
                        "remediated_by": remediated_by or ("kill switch" if abort_reason and "kill" in abort_reason.lower()
                                                           else "controller error" if abort_reason
                                                           else "engine at the planned end"),
                        "agent_note": self._agent_note or None, "observed_breaches": observed_breaches}
                self._set_live(fault_end=fault_end)

            # ---- 6. recovery verification
            if removal_ok:
                self._set_live(state=RunState.VERIFYING_RECOVERY.value,
                               message="fault removed; verifying recovery")
                recovery_ok, recovery_time, rec_notes = self._verify_recovery(run_id, spec, fault_end,
                                                                              cfg.recovery_timeout_s)
                outcome["recovery"] = {"verified": recovery_ok, "recovery_time_s": recovery_time,
                                       "notes": rec_notes}
                self.store.event("recovery_verified" if recovery_ok else "recovery_failed",
                                 rec_notes, run_id, "info" if recovery_ok else "error")
            else:
                outcome["recovery"] = {"verified": False, "recovery_time_s": None,
                                       "notes": "skipped: fault removal not verified"}

            # ---- 7. post-recovery observation
            if recovery_ok:
                phase["v"] = "post"
                self._set_live(state=RunState.OBSERVING.value, phase="post",
                               message=f"post-recovery observation ({t.post_observe_s:.0f}s)")
                end = time.time() + t.post_observe_s
                while time.time() < end:
                    self.safety.heartbeat()
                    time.sleep(min(1.0, t.post_observe_s))
            wl.stop(drain_s=t.post_observe_s if recovery_ok else 0)
            wl_stopped = True
        except _Abort:
            self.store.event("aborted", abort_reason or "aborted", run_id, "warning")
        except Exception as e:  # noqa: BLE001 - last line of defence
            abort_reason = abort_reason or f"controller error: {type(e).__name__}: {e}"
            self.store.event("controller_error", abort_reason, run_id, "error")
            if injected and removal_ok is None:
                removal_ok, notes = self._remove(fault, spec, run_id)
                outcome["fault_removal"] = {"verified": removal_ok, "notes": notes, "at": time.time()}
        finally:
            if not wl_stopped:
                try:
                    wl.stop()
                except Exception:  # noqa: BLE001
                    pass
            self._finish(run_id, cfg, spec, outcome, abort_reason, breaches, injected,
                         removal_ok, recovery_ok, recovery_time, fault_start, fault_end)

    def _remove(self, fault, spec: dict, run_id: str) -> tuple[bool, list[str]]:
        notes: list[str] = []
        for attempt in range(1, self.timings.removal_retries + 1):
            try:
                fault.remove()
                ok, detail = fault.verify_removed()
                notes.append(f"attempt {attempt}: removed, verify -> {detail}")
                if ok:
                    self.store.event("fault_removed", f"verified: {detail}", run_id)
                    self.safety.clear_active()
                    return True, notes
            except Exception as e:  # noqa: BLE001
                notes.append(f"attempt {attempt}: {type(e).__name__}: {e}")
            time.sleep(min(1.0, self.timings.monitor_tick_s))
        # targeted removal failed: sweep everything we know how to undo
        ok, sweep = emergency_rollback(self.docker, spec)
        notes += [f"sweep: {n}" for n in sweep]
        try:
            verified, detail = fault.verify_removed()
        except Exception as e:  # noqa: BLE001
            verified, detail = False, str(e)
        notes.append(f"post-sweep verify -> {detail}")
        if ok and verified:
            self.store.event("fault_removed", "verified after emergency sweep", run_id, "warning")
            self.safety.clear_active()
            return True, notes
        self.store.event("rollback_failed", "; ".join(notes[-3:]), run_id, "error")
        self.safety.block("fault removal could not be verified", run_id)
        return False, notes

    def _verify_recovery(self, run_id: str, spec: dict, fault_end: float,
                         timeout_s: int) -> tuple[bool, Optional[float], str]:
        need = self.timings.recovery_success_streak
        deadline = fault_end + timeout_s
        while time.time() < deadline:
            self.safety.heartbeat()
            try:
                target_ok = self.docker.inspect(spec["container_id"]).ok
            except DockerError:
                try:   # a recreated container (memory fault) has a new id
                    target_ok = self.docker.resolve_service(spec["service"]).ok
                except DockerError:
                    target_ok = False
            rows = [s for s in self.store.samples(run_id, since=fault_end)
                    if s["probe"] in ("api_documents", "api_status")]
            streak = 0
            first_ok_ts = None
            latest: dict[str, bool] = {}
            for s in rows:  # ordered by ts
                latest[s["probe"]] = bool(s["ok"])
                if s["ok"]:
                    streak += 1
                    first_ok_ts = first_ok_ts if streak > 1 else s["ts"]
                else:
                    streak, first_ok_ts = 0, None
            # every recovery probe must have reported, and its newest sample must
            # be good: a streak made of one probe cannot hide another still failing
            all_ok = len(latest) == 2 and all(latest.values())
            if target_ok and all_ok and streak >= need and first_ok_ts is not None:
                rt = max(0.0, first_ok_ts - fault_end)
                return True, rt, (f"target healthy and {streak} consecutive successful app probes; "
                                  f"recovered {rt:.1f}s after fault removal")
            time.sleep(self.timings.monitor_tick_s)
        self.safety.block("application recovery could not be verified", run_id)
        return False, None, f"not recovered within {timeout_s}s (target_ok={target_ok})"

    def _collect_logs(self, spec: dict, since: float) -> dict:
        logs = {}
        try:
            containers = {c.service: c for c in self.docker.project_containers()}
        except DockerError:
            return {}
        for svc in {"webserver", spec["service"]}:
            c = containers.get(svc)
            if not c:
                continue
            try:
                lines = self.docker.logs(c.id, since=since, tail=400)
            except DockerError as e:
                lines = [f"(could not read logs: {e})"]
            relevant = [ln for ln in lines if any(k in ln.lower() for k in LOG_KEYWORDS)]
            logs[svc] = {"relevant": relevant[-80:], "tail": lines[-40:], "total": len(lines)}
        return logs

    def _finish(self, run_id, cfg, spec, outcome, abort_reason, breaches, injected,
                removal_ok, recovery_ok, recovery_time, fault_start, fault_end) -> None:
        try:
            samples = self.store.samples(run_id)
            summary = summarize_samples(samples)
            impact = compare(summary)
            if removal_ok is False or (injected and recovery_ok is False):
                state, result = RunState.FAILED, "cleanup_or_recovery_unverified"
            elif abort_reason and breaches:
                state, result = RunState.ABORTED, "aborted_threshold"
            elif abort_reason:
                state, result = RunState.ABORTED, "aborted"
            elif (outcome.get("remediation") or {}).get("remediated_by") == "agent":
                state, result = RunState.COMPLETED, "remediated_by_agent"
            else:
                state, result = RunState.COMPLETED, "completed"
            failed_checks = [s for s in samples if not s["ok"]][-60:]
            outcome.update({
                "result": result, "abort_reason": abort_reason, "breaches": breaches,
                "injected": injected, "impact": impact,
                "failed_checks": [{k: s[k] for k in ("ts", "phase", "probe", "latency_ms", "status", "detail")}
                                  for s in failed_checks],
                "logs": self._collect_logs(spec, (fault_start or time.time()) - 5) if injected else {},
                "fault_start": fault_start, "fault_end": fault_end,
            })
            self.store.update_run(run_id, state=state.value, ended=time.time(), outcome=outcome,
                                  summary={"metrics": summary,
                                           "health": summarize_health(self.store.health(run_id))})
            self.store.event("finished", f"{result}" + (f": {abort_reason}" if abort_reason else ""),
                             run_id, "info" if state == RunState.COMPLETED else "warning")
            self._set_live(state=state.value, phase="done", result=result, message=result,
                           ended=time.time())
        except Exception as e:  # noqa: BLE001
            self.store.event("finish_error", f"{type(e).__name__}: {e}", run_id, "error")
            self.store.update_run(run_id, state=RunState.FAILED.value, ended=time.time())
            self._set_live(state=RunState.FAILED.value, message=str(e))
        finally:
            self.safety.clear_kill()
            self.safety.release()

    def _start_watchdog(self, spec: dict, deadline: float) -> None:
        if not self.spawn_watchdog:
            return
        kw: dict = {"cwd": str(config.ROOT), "stdin": subprocess.DEVNULL,
                    "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        if os.name == "nt":
            kw["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kw["start_new_session"] = True
        subprocess.Popen([sys.executable, "-m", "chaos.watchdog", "--spec", json.dumps(spec),
                          "--deadline", f"{deadline:.3f}", "--db", str(self.store.path)], **kw)
        self.store.event("watchdog_armed", f"independent watchdog will remove the fault at "
                         f"{datetime.fromtimestamp(deadline, timezone.utc).isoformat()} if still present",
                         spec["run_id"])

    # ------------------------------------------------------------------ baseline
    def start_baseline(self, duration_s: int = 30) -> str:
        if not 10 <= duration_s <= 300:
            raise ExperimentRejected(["baseline duration must be within [10, 300]s"])
        run_id = _run_id("baseline")
        if not self.safety.try_acquire(run_id):
            raise ExperimentRejected(["another experiment or baseline is running"], status=409)
        self.store.create_run(run_id, "baseline", RunState.BASELINE.value,
                              {"duration_s": duration_s, "workload": "v1"})
        with self._live_lock:
            self._live = {"run_id": run_id, "kind": "baseline", "phase": "baseline",
                          "message": f"baseline workload for {duration_s}s", "started": time.time(),
                          "config": {"duration_s": duration_s}}
        self._set_live(state=RunState.BASELINE.value)
        self._thread = threading.Thread(target=self._baseline, args=(run_id, duration_s), daemon=True)
        self._thread.start()
        return run_id

    def _baseline(self, run_id: str, duration_s: int) -> None:
        state = RunState.COMPLETED
        try:
            wl = Workload(self.client_factory(), self.store, run_id, lambda: "baseline",
                          docker=self.docker, **self.workload_kwargs)
            wl.start()
            end = time.time() + duration_s
            while time.time() < end and not self._killed():
                time.sleep(0.5)
            wl.stop(drain_s=30)
            if self._killed():
                state = RunState.ABORTED
            summary = summarize_samples(self.store.samples(run_id))
            self.store.update_run(run_id, state=state.value, ended=time.time(),
                                  summary={"metrics": summary,
                                           "health": summarize_health(self.store.health(run_id))})
            self.store.event("baseline_finished", state.value, run_id)
        except Exception as e:  # noqa: BLE001
            state = RunState.FAILED
            self.store.update_run(run_id, state=state.value, ended=time.time())
            self.store.event("baseline_error", f"{type(e).__name__}: {e}", run_id, "error")
        finally:
            self._set_live(state=state.value, phase="done", message=state.value, ended=time.time())
            self.safety.clear_kill()
            self.safety.release()

    # ------------------------------------------------------------------ kill switch
    def kill(self, reason: str = "manual kill switch") -> dict:
        """Abort whatever is running and immediately remove every fault.
        Does not wait for the experiment thread; its own finally-block
        rollback runs too (removal is idempotent)."""
        self._kill.set()
        self.safety.request_kill(reason)
        live = self.live()
        self.store.event("kill_switch", reason, live.get("run_id"), "warning")
        spec = self.safety.active()
        ok, notes = emergency_rollback(self.docker, spec)
        if ok:
            self.safety.clear_active()
        msg = ("verified: " if ok else "UNVERIFIED: ") + ("; ".join(notes) or "no fault was present")
        self.store.event("kill_rollback", msg, live.get("run_id"), "info" if ok else "error")
        if not ok:
            self.safety.block("kill-switch rollback could not be verified", live.get("run_id"))
        if not self.busy():
            self.safety.clear_kill()
        return {"rollback_verified": ok, "notes": notes, "run_id": live.get("run_id")}

    # ------------------------------------------------------------------ startup / unblock
    def recover_on_startup(self) -> dict:
        """Called when the controller starts. A leftover active-fault marker
        means a previous controller died mid-experiment."""
        self.safety.clear_stale_lock()
        if self.safety.busy():
            holder = self.safety.holder()
            return {"skipped": f"another live controller process holds the lock: {holder}"}
        self.safety.clear_kill()
        report: dict = {"stale_runs": [], "rollback": None}
        for run in self.store.list_runs(limit=200):
            if run["state"] not in {s.value for s in TERMINAL_STATES}:
                self.store.update_run(run["id"], state=RunState.INTERRUPTED.value, ended=time.time())
                report["stale_runs"].append(run["id"])
        spec = self.safety.active()
        if spec:
            self.store.event("crash_recovery", "active fault marker found at startup; rolling back",
                             spec.get("run_id"), "warning", data=spec)
            ok, notes = emergency_rollback(self.docker, spec)
            report["rollback"] = {"verified": ok, "notes": notes}
            if ok:
                self.safety.clear_active()
                self.store.event("crash_recovery_done", "; ".join(notes) or "no fault present",
                                 spec.get("run_id"))
            else:
                self.safety.block("crash recovery could not verify fault removal", spec.get("run_id"))
        return report

    def unblock(self) -> tuple[bool, list[Check]]:
        spec = self.safety.active()
        ok_sweep, notes = emergency_rollback(self.docker, spec)
        checks = [Check("emergency sweep verified", ok_sweep, "; ".join(notes) or "nothing to remove")]
        checks += self.verify_environment()
        ok = all(c.ok for c in checks)
        if ok:
            self.safety.clear_active()
            self.safety.unblock("environment verified healthy; experiments unblocked")
        else:
            self.store.event("unblock_refused", "; ".join(f"{c.name}: {c.detail}" for c in checks if not c.ok),
                             level="warning")
        return ok, checks


class _Abort(Exception):
    pass
