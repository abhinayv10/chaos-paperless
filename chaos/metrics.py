"""Pure summarisation of recorded samples (no I/O)."""
from __future__ import annotations

from collections import Counter, defaultdict
from typing import Iterable, Optional

from .safety import CRITICAL_PROBES, percentile

PHASE_ORDER = ["baseline", "fault", "recovery", "post", "idle"]


def _stats(rows: list[dict]) -> dict:
    lat = [r["latency_ms"] or 0.0 for r in rows]
    ok_lat = [r["latency_ms"] or 0.0 for r in rows if r["ok"]]
    errors = sum(1 for r in rows if not r["ok"])
    return {
        "n": len(rows),
        "errors": errors,
        "error_rate": (errors / len(rows)) if rows else None,
        "p50_ms": percentile(lat, 50),
        "p95_ms": percentile(lat, 95),
        "ok_p95_ms": percentile(ok_lat, 95),
        "mean_ms": (sum(lat) / len(lat)) if lat else None,
        "start": min((r["ts"] for r in rows), default=None),
        "end": max((r["ts"] for r in rows), default=None),
    }


def summarize_samples(samples: Iterable[dict]) -> dict:
    by_phase: dict[str, list[dict]] = defaultdict(list)
    for s in samples:
        by_phase[s["phase"]].append(s)
    out = {}
    for phase in sorted(by_phase, key=lambda p: PHASE_ORDER.index(p) if p in PHASE_ORDER else 99):
        rows = by_phase[phase]
        probes: dict[str, list[dict]] = defaultdict(list)
        for r in rows:
            probes[r["probe"]].append(r)
        out[phase] = {
            "critical": _stats([r for r in rows if r["probe"] in CRITICAL_PROBES]),
            "probes": {p: _stats(v) for p, v in sorted(probes.items())},
        }
    return out


def summarize_health(health: Iterable[dict]) -> dict:
    agg: dict[str, dict[str, Counter]] = defaultdict(lambda: defaultdict(Counter))
    for h in health:
        label = h["state"] if h["state"] != "running" else f"running/{h['health']}"
        agg[h["phase"]][h["service"]][label] += 1
    return {ph: {svc: dict(c) for svc, c in svcs.items()} for ph, svcs in agg.items()}


def _fmt_pct(v: Optional[float]) -> str:
    return "n/a" if v is None else f"{v:.0%}"


def _fmt_ms(v: Optional[float]) -> str:
    return "n/a" if v is None else f"{v:.0f}ms"


def compare(summary: dict) -> dict:
    """Impact of the fault relative to the pre-injection baseline."""
    base = summary.get("baseline", {}).get("critical")
    fault = summary.get("fault", {}).get("critical")
    obs: list[str] = []
    if not base or not fault or not base["n"] or not fault["n"]:
        return {"impact_detected": None, "observations": ["insufficient samples to compare"]}
    err_delta = (fault["error_rate"] or 0) - (base["error_rate"] or 0)
    p95_ratio = (fault["p95_ms"] / base["p95_ms"]) if base["p95_ms"] else None
    obs.append(f"critical error rate {_fmt_pct(base['error_rate'])} -> {_fmt_pct(fault['error_rate'])}")
    obs.append(f"critical p95 latency {_fmt_ms(base['p95_ms'])} -> {_fmt_ms(fault['p95_ms'])}")
    for probe, st in summary["fault"]["probes"].items():
        b = summary["baseline"]["probes"].get(probe)
        if b and st["n"]:
            if (st["error_rate"] or 0) > (b["error_rate"] or 0) or (
                    b["p95_ms"] and st["p95_ms"] and st["p95_ms"] > 2 * b["p95_ms"]):
                obs.append(f"{probe}: errors {_fmt_pct(b['error_rate'])} -> {_fmt_pct(st['error_rate'])}, "
                           f"p95 {_fmt_ms(b['p95_ms'])} -> {_fmt_ms(st['p95_ms'])}")
    dp = summary["fault"]["probes"].get("doc_processing")
    if dp and dp["errors"]:
        obs.append(f"{dp['errors']}/{dp['n']} documents submitted during the fault failed or did not finish in time")
    elif dp and dp["n"]:
        obs.append(f"documents submitted during the fault took p95 {_fmt_ms(dp['p95_ms'])} end-to-end")
    impact = err_delta > 0.10 or (p95_ratio is not None and p95_ratio > 2.0) or bool(dp and dp["errors"])
    return {
        "impact_detected": impact,
        "error_rate_delta": err_delta,
        "p95_ratio": p95_ratio,
        "observations": obs,
    }
