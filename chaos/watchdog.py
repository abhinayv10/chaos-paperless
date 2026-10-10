"""Independent dead-man's switch.

Spawned as a separate, detached OS process when a fault is injected. It
knows only the fault spec and a hard deadline. If the controller has not
recorded fault removal by the deadline (because it crashed, hung, or was
killed), the watchdog removes the fault itself, verifies, and blocks further
experiments if verification fails. It does not depend on the API server,
the dashboard, or the experiment thread.
"""
from __future__ import annotations

import argparse
import json
import time

from .docker_ops import Docker
from .faults import build_fault, emergency_rollback
from .safety import SafetyState
from .storage import Store

MAX_LIFETIME_S = 600


def watch(store: Store, docker: Docker, spec: dict, deadline: float, poll_s: float = 1.0,
          max_lifetime_s: float = MAX_LIFETIME_S) -> str:
    run_id = spec["run_id"]
    started = time.time()
    while time.time() < deadline:
        run = store.get_run(run_id)
        if run and run.get("fault_end"):
            return "controller removed fault"
        if time.time() - started > max_lifetime_s:
            break
        time.sleep(poll_s)
    run = store.get_run(run_id)
    if run and run.get("fault_end"):
        return "controller removed fault"
    store.event("watchdog_rollback", "deadline passed without recorded fault removal; removing fault",
                run_id, "error", data=spec)
    safety = SafetyState(store)
    try:
        fault = build_fault(docker, spec)
        fault.remove()
        ok, detail = fault.verify_removed()
    except Exception as e:  # noqa: BLE001
        ok, detail = False, f"{type(e).__name__}: {e}"
    if not ok:
        swept, notes = emergency_rollback(docker, spec)
        ok, detail = swept, "; ".join(notes)
    if ok:
        safety.clear_active()
        store.event("watchdog_rollback_done", f"fault removed by watchdog: {detail}", run_id, "warning")
        return "watchdog removed fault"
    safety.block("watchdog could not verify fault removal", run_id)
    return "watchdog removal FAILED"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", required=True)
    ap.add_argument("--deadline", type=float, required=True)
    ap.add_argument("--db", required=True)
    a = ap.parse_args()
    print(watch(Store(a.db), Docker(), json.loads(a.spec), a.deadline))


if __name__ == "__main__":
    main()
