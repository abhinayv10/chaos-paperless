"""Deterministic severity and resilience scoring (no LLM, no I/O).

SEVERITY of one experiment's finding, 0-100 (higher = worse):
    30%  breadth      share of measured workflows affected (failed = 1, degraded = 0.5)
    25%  criticality  the same share weighted by workflow criticality (catalog.WORKFLOWS)
    25%  magnitude    max(critical error-rate increase,
                          slowdown of *successful* requests: (p95 ratio - 1) / 9, capped at 1)
    20%  recovery     recovery time / 60 s capped at 1; 1.0 if removal or recovery unverified
  Categories: critical >= 75, high >= 50, medium >= 25, low > 5, else none.
  An unverified rollback or recovery floors the score at 90 (critical).

RESILIENCE of one experiment, 0-100 (higher = better):
    50%  availability  1 - criticality-weighted error-rate increase during the fault
    20%  performance   1 - mean slowdown of successful requests ((ok p95 ratio - 1) / 9, capped)
    30%  recovery      1 - recovery time / 60 s (floored at 0); 0 if unverified
  A component with no data is dropped and the remaining weights renormalised;
  the dropped component is listed, never assumed to be perfect.

OVERALL resilience = mean of the latest scored run per (fault, target)
scenario. Inconclusive runs are counted but never scored, and a run cut short
by the operator (kill switch) never replaces an earlier scored run of the same
scenario. If any scenario's latest run left recovery or rollback unverified,
the overall score is capped at 25. Coverage (scenarios scored / scenarios in the catalogue) is reported
alongside: a small catalogue says nothing about faults it does not contain.
"""
from __future__ import annotations

from typing import Optional

SEVERITY_WEIGHTS = {"breadth": 0.30, "criticality": 0.25, "magnitude": 0.25, "recovery": 0.20}
RESILIENCE_WEIGHTS = {"availability": 0.50, "performance": 0.20, "recovery": 0.30}
RECOVERY_NORM_S = 60.0
SLOWDOWN_NORM = 9.0  # a 10x slowdown saturates the latency component
UNRECOVERED_SEVERITY_FLOOR = 90
UNRECOVERED_OVERALL_CAP = 25
CATEGORIES = [(75, "critical"), (50, "high"), (25, "medium"), (5.0001, "low")]


def _clamp(v: float) -> float:
    return max(0.0, min(1.0, v))


def _slowdown(ratio: Optional[float]) -> Optional[float]:
    return None if ratio is None else _clamp((ratio - 1.0) / SLOWDOWN_NORM)


def category(score: Optional[float]) -> str:
    if score is None:
        return "inconclusive"
    return next((name for floor, name in CATEGORIES if score >= floor), "none")


def unrecovered(a: dict) -> bool:
    return bool(a.get("injected")) and (a.get("removal_verified") is False
                                        or a.get("recovery_verified") is not True)


def _recovery_penalty(a: dict) -> Optional[float]:
    if unrecovered(a):
        return 1.0
    t = a.get("recovery_time_s")
    return None if t is None else _clamp(t / RECOVERY_NORM_S)


def _weighted(components: dict[str, Optional[float]], weights: dict[str, float]) -> tuple[Optional[float], list[str]]:
    present = {k: v for k, v in components.items() if v is not None}
    missing = [k for k, v in components.items() if v is None]
    total = sum(weights[k] for k in present)
    if not present or total == 0:
        return None, missing
    return sum(weights[k] * v for k, v in present.items()) / total, missing


def severity(a: dict) -> dict:
    """a = analysis.analyze_run() output."""
    if a["impact"] == "inconclusive":
        return {"score": None, "category": "inconclusive", "components": {}, "missing": [],
                "reason": a["impact_reason"], "weights": SEVERITY_WEIGHTS}
    measured = [w for w in a["workflows"] if w["status"] != "no_data"]
    hit = {"failed": 1.0, "degraded": 0.5, "functional": 0.0}
    breadth = sum(hit[w["status"]] for w in measured) / len(measured)
    crit_total = sum(w["criticality"] for w in measured)
    criticality = sum(hit[w["status"]] * w["criticality"] for w in measured) / crit_total
    err = _clamp(a["critical"]["error_rate_delta"] or 0.0)
    slow = [s for s in (_slowdown(w["ok_p95_ratio"]) for w in measured) if s is not None]
    magnitude = max([err] + slow)
    components = {"breadth": breadth, "criticality": criticality, "magnitude": magnitude,
                  "recovery": _recovery_penalty(a)}
    value, missing = _weighted(components, SEVERITY_WEIGHTS)
    score = round(100 * value, 1)
    floored = False
    if unrecovered(a) and score < UNRECOVERED_SEVERITY_FLOOR:
        score, floored = float(UNRECOVERED_SEVERITY_FLOOR), True
    return {"score": score, "category": category(score), "components": components, "missing": missing,
            "floored_unrecovered": floored, "weights": SEVERITY_WEIGHTS,
            "reason": "rollback or recovery unverified" if floored else ""}


def resilience(a: dict) -> dict:
    if a["impact"] == "inconclusive":
        return {"score": None, "components": {}, "missing": [], "reason": a["impact_reason"],
                "weights": RESILIENCE_WEIGHTS}
    measured = [w for w in a["workflows"] if w["status"] != "no_data"]
    crit_total = sum(w["criticality"] for w in measured)
    lost = sum(w["criticality"] * _clamp(w["error_rate_delta"] or 0.0) for w in measured) / crit_total
    slow = [s for s in (_slowdown(w["ok_p95_ratio"]) for w in measured) if s is not None]
    pen = _recovery_penalty(a)
    components = {"availability": 1.0 - lost,
                  "performance": (1.0 - sum(slow) / len(slow)) if slow else None,
                  "recovery": None if pen is None else 1.0 - pen}
    value, missing = _weighted(components, RESILIENCE_WEIGHTS)
    return {"score": None if value is None else round(100 * value, 1), "components": components,
            "missing": missing, "weights": RESILIENCE_WEIGHTS,
            "reason": "; ".join(f"no data for {m}" for m in missing)}


def status_of(a: dict) -> str:
    """completed | aborted | inconclusive | unrecovered: how a run counts."""
    if unrecovered(a):
        return "unrecovered"
    if a["impact"] == "inconclusive":
        return "inconclusive"
    return "aborted" if a.get("truncated") else "completed"


def overall(findings: list[dict], scenarios: list[tuple[str, str]]) -> dict:
    """findings: analyst findings ordered oldest -> newest (each has
    'deterministic', 'severity', 'resilience')."""
    latest: dict[tuple[str, str], dict] = {}
    counts = {"completed": 0, "aborted": 0, "inconclusive": 0, "unrecovered": 0}
    for f in findings:
        d = f["deterministic"]
        st = status_of(d)
        counts[st] += 1
        key = (d["fault_type"], d["target"])
        # an inconclusive or operator-interrupted rerun must not erase an
        # earlier scored result; an unrecovered one always counts
        weak = st == "inconclusive" or (d.get("abort_kind") == "manual" and st != "unrecovered")
        if not weak or key not in latest:
            latest[key] = f
    scored = {k: f for k, f in latest.items() if f["resilience"]["score"] is not None}
    unrec = [f["run_id"] for f in latest.values() if unrecovered(f["deterministic"])]
    score = round(sum(f["resilience"]["score"] for f in scored.values()) / len(scored), 1) if scored else None
    capped = False
    if score is not None and unrec and score > UNRECOVERED_OVERALL_CAP:
        score, capped = float(UNRECOVERED_OVERALL_CAP), True
    components: dict[str, Optional[float]] = {}
    for name in RESILIENCE_WEIGHTS:
        vals = [f["resilience"]["components"].get(name) for f in scored.values()]
        vals = [v for v in vals if v is not None]
        components[name] = round(sum(vals) / len(vals), 3) if vals else None
    covered = [k for k in scenarios if k in scored]
    return {
        "score": score, "capped_unrecovered": capped, "unrecovered_runs": unrec,
        "components": components, "weights": RESILIENCE_WEIGHTS,
        "experiments_analysed": len(findings), "counts": counts,
        "scenarios_scored": len(scored), "scenarios_total": len(scenarios),
        "coverage": round(len(covered) / len(scenarios), 3) if scenarios else None,
        "untested_scenarios": [list(k) for k in scenarios if k not in scored],
        "per_scenario": [{"fault_type": k[0], "target": k[1], "run_id": f["run_id"],
                          "resilience": f["resilience"]["score"], "severity": f["severity"]["score"],
                          "severity_category": f["severity"]["category"],
                          "status": status_of(f["deterministic"])}
                         for k, f in sorted(latest.items())],
        "note": ("Score covers only the scenarios listed; it is not a statement about faults "
                 "outside this catalogue." if scored else "No experiment produced scoreable evidence yet."),
    }
