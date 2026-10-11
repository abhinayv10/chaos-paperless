"""Sequential execution of planner-approved experiments.

This module adds no fault or rollback logic. Each experiment goes through
Engine.start(), so validation, preflight, thresholds, the watchdog, rollback
and recovery checks are exactly the Day 1 ones, applied afresh to every run.

The sequence stops (and says why) when an experiment is rejected, leaves the
environment BLOCKED or unrecovered, is interrupted, or when the kill switch
is used. Analysis and export run only after a run has fully finished, inside
their own try/except: an AI or disk failure cannot affect cleanup.
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Optional

from .engine import Engine, ExperimentRejected
from .planner import to_config
from .storage import Store

SETTLE_S = 5.0


class Orchestrator:
    def __init__(self, engine: Engine, store: Store,
                 after_run: Optional[Callable[[str], None]] = None, settle_s: float = SETTLE_S,
                 prepare: Optional[Callable] = None):
        self.engine = engine
        self.store = store
        self.after_run = after_run   # e.g. analyse + export; failures are recorded, not raised
        self.settle_s = settle_s
        self.prepare = prepare       # e.g. switch the run to self-healing mode
        self._stop = threading.Event()
        self._stop_reason = ""
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def stop(self, reason: str = "stopped") -> None:
        """Ask the sequence to end before its next experiment. The running
        experiment itself is aborted by the engine's kill switch, not here."""
        self._stop_reason = reason
        self._stop.set()

    def status(self) -> dict:
        plan = self.store.latest_plan()
        return {"running": self.running(), "execution": (plan or {}).get("execution"),
                "plan_id": (plan or {}).get("id")}

    # ------------------------------------------------------------------------
    def start(self, plan_id: str, ranks: list[int] | None = None) -> dict:
        """Validate the selection, then run it on a background thread."""
        with self._lock:
            if self.running():
                raise ExperimentRejected(["a plan sequence is already running"], status=409)
            plan, selected = self._select(plan_id, ranks)
            self._stop.clear()
            self._thread = threading.Thread(target=self.run_sequence, args=(plan_id, ranks),
                                            name=f"sequence-{plan_id}", daemon=True)
            self._thread.start()
        return {"plan_id": plan["id"], "ranks": [r["rank"] for r in selected]}

    def wait(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)

    def _select(self, plan_id: str, ranks: list[int] | None) -> tuple[dict, list[dict]]:
        plan = self.store.get_plan(plan_id)
        if not plan:
            raise ExperimentRejected([f"no such plan '{plan_id}'"], status=404)
        recs = plan["recommendations"]
        if ranks:
            unknown = sorted(set(ranks) - {r["rank"] for r in recs})
            if unknown:
                raise ExperimentRejected([f"plan has no recommendation with rank {unknown}"])
            recs = [r for r in recs if r["rank"] in set(ranks)]
        if not recs:
            raise ExperimentRejected(["no recommendations selected"])
        return plan, sorted(recs, key=lambda r: r["rank"])

    def run_sequence(self, plan_id: str, ranks: list[int] | None = None) -> dict:
        """Run the selected recommendations one after another (blocking)."""
        plan, selected = self._select(plan_id, ranks)
        execution = {"started": time.time(), "ended": None, "status": "running", "stop_reason": None,
                     "selected": [r["rank"] for r in selected], "current": None, "steps": []}
        plan["execution"] = execution

        def save() -> None:
            self.store.save_plan(plan)

        self.store.event("sequence_started", f"{plan_id}: ranks {execution['selected']}")
        stop_reason = None
        for i, rec in enumerate(selected):
            step = {"rank": rec["rank"], "name": rec["name"], "run_id": None, "state": None,
                    "result": None, "errors": [], "analysis_error": None}
            execution["current"] = rec["rank"]
            # ---- safety gate before every run (the engine re-checks all of it too)
            if self._stop.is_set():
                stop_reason = f"stopped before rank {rec['rank']}: {self._stop_reason}"
            elif self.engine.safety.blocked():
                stop_reason = (f"environment BLOCKED before rank {rec['rank']}: "
                               f"{self.engine.safety.blocked().get('reason')}")
            if stop_reason:
                break
            try:
                cfg = to_config(rec)
                run_id = self.engine.start(self.prepare(cfg) if self.prepare else cfg)
            except ExperimentRejected as e:
                step.update(state="rejected", result="rejected", errors=e.errors)
                rec["status"] = "rejected"
                execution["steps"].append(step)
                stop_reason = f"rank {rec['rank']} rejected by safety checks: {'; '.join(e.errors)[:300]}"
                break
            except Exception as e:  # noqa: BLE001 - e.g. a malformed stored plan
                step.update(state="rejected", result="error", errors=[f"{type(e).__name__}: {e}"])
                rec["status"] = "rejected"
                execution["steps"].append(step)
                stop_reason = f"rank {rec['rank']} could not be started: {type(e).__name__}"
                break
            step["run_id"], rec["run_id"], rec["status"] = run_id, run_id, "running"
            execution["steps"].append(step)
            save()
            self.engine.wait()
            run = self.store.get_run(run_id) or {}
            out = run.get("outcome") or {}
            step.update(state=run.get("state"), result=out.get("result"))
            rec["status"] = "executed"
            save()
            if self.after_run:
                try:
                    self.after_run(run_id)
                except Exception as e:  # noqa: BLE001 - AI/export failure never stops safety logic
                    step["analysis_error"] = f"{type(e).__name__}: {e}"
                    self.store.event("analysis_error", step["analysis_error"], run_id, "warning")
            # ---- may the next experiment start?
            abort = out.get("abort_reason") or ""
            if self.engine.safety.blocked():
                stop_reason = f"rank {rec['rank']} left the environment BLOCKED"
            elif run.get("state") in ("failed", "interrupted", None):
                stop_reason = f"rank {rec['rank']} ended in state {run.get('state')}"
            elif (out.get("recovery") or {}).get("verified") is False:
                stop_reason = f"rank {rec['rank']}: recovery not verified"
            elif "kill" in abort.lower() or self._stop.is_set():
                stop_reason = f"kill switch used during rank {rec['rank']}"
            if stop_reason:
                break
            save()
            if i < len(selected) - 1:
                self._stop.wait(self.settle_s)
        done = {s["rank"] for s in execution["steps"]}
        for rec in selected:
            if rec["rank"] not in done:
                rec["status"] = "skipped"
        execution.update(ended=time.time(), current=None, stop_reason=stop_reason,
                         status="stopped" if stop_reason else "completed")
        save()
        self.store.event("sequence_finished", f"{plan_id}: {execution['status']}"
                         + (f" ({stop_reason})" if stop_reason else ""),
                         level="warning" if stop_reason else "info")
        return execution
