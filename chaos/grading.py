"""After-the-fact grading of the blind self-healing agent, and the measured
circuit-breaker check. Runs outside the agent: this is the only place the
agent's diagnosis meets what was really injected."""
from __future__ import annotations

from typing import Optional

from . import detection
from .storage import Store


def grade_incident(store: Store, inc: dict) -> dict:
    run = store.get_run(inc["run_id"]) or {}
    cfg, out = run.get("config") or {}, run.get("outcome") or {}
    fs = run.get("fault_start")
    expected = detection.expected_class(cfg.get("fault_type"), cfg.get("target")) if fs else None
    atts = inc.get("attempts") or []
    final = atts[-1]["diagnosis"]["failure_class"] if atts else None
    since = lambda t: round(t - fs, 1) if t and fs else None  # noqa: E731
    # was there anything for the agent to see? (it watches user-facing workflows only)
    det = (store.get_analysis(inc["run_id"]) or {}).get("deterministic") or {}
    visible = [w for w in det.get("failed", []) + det.get("degraded", []) if w in detection.PROBES]
    quiet = bool(fs and inc.get("status") == "no_incident")
    return {
        "run_state": run.get("state"), "run_result": out.get("result"),
        "injected": {"fault_type": cfg.get("fault_type"), "target": cfg.get("target"),
                     "latency_ms": cfg.get("latency_ms"), "loss_pct": cfg.get("loss_pct"),
                     "cpu_limit": cfg.get("cpu_limit"), "planned_s": cfg.get("duration_s"),
                     "fault_start": fs, "fault_end": run.get("fault_end")},
        "expected_class": expected,
        "final_class": final,
        "first_class": atts[0]["diagnosis"]["failure_class"] if atts else None,
        "correct": (final == expected) if final and expected else None,
        "first_correct": (atts[0]["diagnosis"]["failure_class"] == expected) if atts and expected else None,
        "time_to_detect_s": since(inc.get("detected_at")),
        "time_to_diagnose_s": since(atts[0]["started"]) if atts else None,
        "time_to_fix_s": since(atts[-1].get("remediated_at")) if atts else None,
        "time_to_verified_s": since(inc.get("resolved_at")),
        "remediated_by": (out.get("remediation") or {}).get("remediated_by"),
        "detected_before_fault": bool(inc.get("detected_at") and fs and inc["detected_at"] < fs),
        "visible_impact": visible if det else None,
        # no incident is only a miss if the post-run analysis found user-facing impact
        "missed": quiet and (not det or bool(visible)),
        "correct_no_incident": quiet and bool(det) and not visible,
    }


def fail_fast(samples: list[dict]) -> Optional[dict]:
    """Circuit-breaker validation from measurements: do failed requests get
    faster as the outage goes on (breaker-like fast-fail), or does every
    request keep waiting for its own timeout?"""
    fails = sorted((s for s in samples if s["phase"] == "fault" and not s["ok"] and s["probe"] in detection.PROBES),
                   key=lambda s: s["ts"])
    if len(fails) < 4:
        return None
    k = max(1, len(fails) // 3)
    mean = lambda rows: sum(r["latency_ms"] or 0 for r in rows) / len(rows)  # noqa: E731
    early, late = mean(fails[:k]), mean(fails[-k:])
    if early <= 500:
        verdict, ok = ("failures were fast from the start (the connection was refused), so callers were not "
                       "kept waiting; a breaker would mainly cut load on the dependency"), True
    elif late <= max(500, 0.5 * early):
        verdict, ok = "failures became fast as the outage went on: breaker-like fast-fail behaviour", True
    else:
        verdict, ok = ("no fast-fail: requests kept waiting about as long at the end of the outage as at the start; "
                       "a circuit breaker would have turned these waits into immediate errors"), False
    return {"failed_requests": len(fails), "early_ms": round(early), "late_ms": round(late),
            "fails_fast": ok, "verdict": verdict}
