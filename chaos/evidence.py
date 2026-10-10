"""Structured evidence record for one experiment, and its JSON export.

SQLite stays the single source of truth: the record is assembled from the
stored run on demand and the export file is just a rendering of it. Fields
that were not measured are null, never zero or 'ok'.
"""
from __future__ import annotations

import json
from pathlib import Path

from .storage import Store

SCHEMA = "chaos-evidence/v1"
SAFETY_EVENTS = ("preflight_passed", "rejected", "fault_injected", "watchdog_armed", "threshold_breach",
                 "manual_abort", "kill_switch", "kill_rollback", "fault_error", "fault_removed",
                 "rollback_failed", "recovery_verified", "recovery_failed", "blocked", "aborted",
                 "controller_error", "watchdog_rollback", "watchdog_rollback_done", "finished")


def build_evidence(store: Store, run_id: str) -> dict | None:
    run = store.get_run(run_id)
    if not run or run["kind"] != "experiment":
        return None
    cfg = run.get("config") or {}
    out = run.get("outcome") or {}
    summary = run.get("summary") or {}
    fs, fe = run.get("fault_start"), run.get("fault_end")
    events = list(reversed(store.events(run_id, limit=300)))
    return {
        "schema": SCHEMA,
        "run_id": run_id,
        "state": run["state"],
        "result": out.get("result"),
        "config": cfg,
        "target": cfg.get("target"),
        "fault": {"type": cfg.get("fault_type"), "latency_ms": cfg.get("latency_ms"),
                  "planned_duration_s": cfg.get("duration_s"), "injected": out.get("injected"),
                  "actual_duration_s": (fe - fs) if fs and fe else None},
        "timestamps": {"created": run.get("created"), "fault_start": fs, "fault_end": fe,
                       "ended": run.get("ended")},
        "hypothesis": cfg.get("hypothesis") or None,
        "preflight": out.get("preflight"),
        "metrics": summary.get("metrics"),          # per phase: critical + per probe
        "container_health": summary.get("health"),
        "impact": out.get("impact"),
        "failed_checks": out.get("failed_checks"),
        "abort": {"reason": out.get("abort_reason"), "breaches": out.get("breaches")},
        "rollback": out.get("fault_removal"),
        "recovery": out.get("recovery"),
        "logs": out.get("logs"),
        "safety_events": [{k: e[k] for k in ("ts", "level", "kind", "message")}
                          for e in events if e["kind"] in SAFETY_EVENTS],
        "errors": [{k: e[k] for k in ("ts", "kind", "message")} for e in events if e["level"] == "error"],
        "sample_count": len(store.samples(run_id)),
    }


def export_run(store: Store, run_id: str, directory: Path | None = None) -> Path | None:
    """Write one JSON file per experiment: evidence plus the analysis, if any."""
    ev = build_evidence(store, run_id)
    if ev is None:
        return None
    # default: next to the database, so a test or alternate store never writes elsewhere
    directory = Path(directory) if directory else store.path.parent / "exports"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{run_id}.json"
    path.write_text(json.dumps({"evidence": ev, "analysis": store.get_analysis(run_id)}, indent=2),
                    encoding="utf-8")
    return path
