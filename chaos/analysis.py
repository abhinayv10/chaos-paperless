"""Deterministic evidence analysis (no LLM, no I/O).

Turns one persisted experiment run into per-workflow outcomes, an impact
classification and a numbered list of facts. The analyst agent may only cite
these facts, so every claim in a finding traces back to a measurement.

Rules (fault phase compared with the pre-injection baseline of the same run):
  failed      error rate >= 50% and at least 40 points above baseline
  degraded    error rate > 10 points above baseline, or p95 latency more than
              2x baseline and more than 200 ms slower
  functional  neither of the above
  no_data     no samples in the baseline or the fault phase
Impact is 'full' when every workflow with data failed, 'partial' when some
failed or degraded, 'none' when all stayed functional, and 'inconclusive'
when the fault was not injected or fewer than MIN_FAULT_SAMPLES critical
probes finished during the fault.
"""
from __future__ import annotations

from collections import Counter
from typing import Optional

from .catalog import WORKFLOWS

MIN_FAULT_SAMPLES = 3
FAILED_RATE, FAILED_DELTA = 0.5, 0.4
DEGRADED_DELTA = 0.10
DEGRADED_P95_RATIO, DEGRADED_P95_ABS_MS = 2.0, 200.0


def _ratio(a: Optional[float], b: Optional[float]) -> Optional[float]:
    return (a / b) if a is not None and b else None


def workflow_outcome(name: str, base: Optional[dict], fault: Optional[dict]) -> dict:
    spec = WORKFLOWS[name]
    out = {"workflow": name, "label": spec["label"], "criticality": spec["criticality"],
           "expected_path": spec["expected_path"], "baseline": base, "fault": fault,
           "error_rate_delta": None, "p95_ratio": None, "ok_p95_ratio": None, "low_sample": False}
    if not base or not fault or not base.get("n") or not fault.get("n"):
        missing = "baseline" if not base or not base.get("n") else "fault"
        return out | {"status": "no_data", "reason": f"no {missing}-phase samples"}
    delta = fault["error_rate"] - base["error_rate"]
    p95_ratio = _ratio(fault.get("p95_ms"), base.get("p95_ms"))
    out.update(error_rate_delta=delta, p95_ratio=p95_ratio,
               ok_p95_ratio=_ratio(fault.get("ok_p95_ms"), base.get("ok_p95_ms")),
               low_sample=fault["n"] < MIN_FAULT_SAMPLES)
    slower = (p95_ratio is not None and p95_ratio > DEGRADED_P95_RATIO
              and fault["p95_ms"] - base["p95_ms"] > DEGRADED_P95_ABS_MS)
    if fault["error_rate"] >= FAILED_RATE and delta >= FAILED_DELTA:
        status = "failed"
    elif delta > DEGRADED_DELTA or slower:
        status = "degraded"
    else:
        status = "functional"
    return out | {"status": status, "reason": ""}


def _pct(v) -> str:
    return "n/a" if v is None else f"{v:.0%}"


def _ms(v) -> str:
    return "n/a" if v is None else f"{v:.0f}ms"


def analyze_run(run: dict, edges: list[dict] | None = None) -> dict:
    """Deterministic analysis of one experiment run row (as stored)."""
    cfg = run.get("config") or {}
    out = run.get("outcome") or {}
    metrics = (run.get("summary") or {}).get("metrics") or {}
    health = (run.get("summary") or {}).get("health") or {}
    target = cfg.get("target")
    base_p, fault_p = (metrics.get(p, {}).get("probes", {}) for p in ("baseline", "fault"))
    base_c, fault_c = (metrics.get(p, {}).get("critical") for p in ("baseline", "fault"))
    workflows = [workflow_outcome(w, base_p.get(w), fault_p.get(w)) for w in WORKFLOWS]
    with_data = [w for w in workflows if w["status"] != "no_data"]
    failed = [w["workflow"] for w in with_data if w["status"] == "failed"]
    degraded = [w["workflow"] for w in with_data if w["status"] == "degraded"]
    functional = [w["workflow"] for w in with_data if w["status"] == "functional"]

    injected = bool(out.get("injected"))
    removal = (out.get("fault_removal") or {}).get("verified")
    rec = out.get("recovery") or {}
    recovered = rec.get("verified")
    fault_n = (fault_c or {}).get("n") or 0
    limitations: list[str] = []
    if not injected:
        impact, why = "inconclusive", "no fault was injected in this run"
    elif not (base_c or {}).get("n"):
        impact, why = "inconclusive", "no baseline samples to compare against"
    elif fault_n < MIN_FAULT_SAMPLES:
        impact, why = "inconclusive", (f"only {fault_n} critical probes finished during the fault "
                                       f"(need {MIN_FAULT_SAMPLES})")
    elif failed and len(failed) == len(with_data):
        impact, why = "full", f"all {len(with_data)} measured workflows failed"
    elif failed or degraded:
        impact, why = "partial", (f"{len(failed)} failed, {len(degraded)} degraded, "
                                  f"{len(functional)} still functional of {len(with_data)} measured")
    else:
        impact, why = "none", f"all {len(with_data)} measured workflows stayed functional"

    fs, fe = out.get("fault_start"), out.get("fault_end")
    actual = (fe - fs) if fs and fe else None
    truncated = bool(out.get("abort_reason")) and injected
    # who cut the fault short: the safety thresholds, or an operator / error
    abort_kind = None if not truncated else ("threshold" if out.get("breaches") else "manual")
    if truncated and actual is not None:
        limitations.append(f"fault window truncated to {actual:.1f}s of the planned "
                           f"{cfg.get('duration_s')}s by an abort; later-stage effects were not observed")
    for w in workflows:
        if w["status"] == "no_data":
            limitations.append(f"{w['workflow']}: {w['reason']}")
        elif w["low_sample"]:
            limitations.append(f"{w['workflow']}: only {w['fault']['n']} fault-phase sample(s)")
    if injected and recovered is None:
        limitations.append("recovery was not measured")

    expected = [w for w in WORKFLOWS if target in WORKFLOWS[w]["expected_path"]]
    affected = failed + degraded
    surprises = {
        "affected_but_not_expected": [w for w in affected if w not in expected],
        "expected_but_unaffected": [w for w in expected if w in functional],
    }

    # ---- numbered facts: the only things the analyst agent may cite
    facts: list[dict] = []

    def fact(kind: str, text: str) -> None:
        facts.append({"id": f"F{len(facts) + 1}", "kind": kind, "text": text})

    lat = f" {cfg['latency_ms']}ms" if cfg.get("latency_ms") else ""
    if injected:
        fact("fault", f"{cfg.get('fault_type')}{lat} was injected on '{target}' and was active for "
             + (f"{actual:.1f}s" if actual is not None else "an unrecorded time")
             + f" (planned {cfg.get('duration_s')}s).")
    else:
        fact("fault", f"No fault was injected (run result: {out.get('result') or run.get('state')}).")
    if out.get("abort_reason"):
        fact("abort", f"The run was aborted: {out['abort_reason']}.")
    if base_c and fault_c and base_c.get("n") and fault_c.get("n"):
        fact("metric", f"All critical probes: error rate {_pct(base_c['error_rate'])} at baseline -> "
             f"{_pct(fault_c['error_rate'])} during the fault (n={base_c['n']} -> {fault_c['n']}); "
             f"p95 latency {_ms(base_c['p95_ms'])} -> {_ms(fault_c['p95_ms'])}.")
    for w in workflows:
        if w["status"] == "no_data":
            fact("workflow", f"{w['workflow']}: not measured ({w['reason']}).")
            continue
        b, f = w["baseline"], w["fault"]
        fact("workflow", f"{w['workflow']} ({w['label']}) {w['status'].upper()}: error rate "
             f"{_pct(b['error_rate'])} -> {_pct(f['error_rate'])} (n={b['n']} -> {f['n']}), "
             f"p95 {_ms(b['p95_ms'])} -> {_ms(f['p95_ms'])}.")
    details = Counter((s["probe"], _normalise(s.get("detail") or "")) for s in out.get("failed_checks") or []
                      if s.get("phase") == "fault")
    for (probe, detail), n in details.most_common(6):
        fact("failure_mode", f"{probe} failed {n}x during the fault with: {detail or '(no detail)'}.")
    for svc, states in sorted((health.get("fault") or {}).items()):
        fact("health", f"Container '{svc}' during the fault: "
             + ", ".join(f"{k} x{v}" for k, v in sorted(states.items())) + ".")
    for e in edges or []:
        if target in (e.get("from"), e.get("to")):
            fact("dependency", f"Dependency map: {e['from']} -> {e['to']} "
                 f"({'+'.join(e.get('kinds', []))}; {'; '.join(e.get('evidence', []))[:160]}).")
    if injected and impact != "inconclusive":
        fact("correlation", f"Workflows expected to use '{target}': {expected or 'none'}; observed affected: "
             f"{affected or 'none'}; affected but not expected: "
             f"{surprises['affected_but_not_expected'] or 'none'}; expected but unaffected: "
             f"{surprises['expected_but_unaffected'] or 'none'}.")
    if injected:
        fact("rollback", f"Fault removal verified: {removal}.")
        if recovered:
            fact("recovery", f"Application recovery verified {rec.get('recovery_time_s'):.1f}s after fault removal."
                 if rec.get("recovery_time_s") is not None else "Application recovery verified (time not recorded).")
        else:
            fact("recovery", f"Application recovery NOT verified: {rec.get('notes') or 'no recovery record'}.")
    post = (metrics.get("post") or {}).get("critical")
    if post and post.get("n"):
        fact("metric", f"Post-recovery observation: error rate {_pct(post['error_rate'])}, "
             f"p95 {_ms(post['p95_ms'])} over {post['n']} critical probes.")
    logs = out.get("logs") or {}
    for svc, entry in sorted(logs.items()):
        fact("logs", f"{len(entry.get('relevant') or [])} of {entry.get('total', 0)} '{svc}' log lines since "
             f"the fault matched error keywords.")

    return {
        "run_id": run["id"], "target": target, "fault_type": cfg.get("fault_type"),
        "latency_ms": cfg.get("latency_ms"), "injected": injected,
        "result": out.get("result") or run.get("state"), "truncated": truncated,
        "abort_kind": abort_kind,
        "fault_duration_s": actual, "planned_duration_s": cfg.get("duration_s"),
        "workflows": workflows, "failed": failed, "degraded": degraded, "functional": functional,
        "impact": impact, "impact_reason": why,
        "critical": {"baseline": base_c, "fault": fault_c,
                     "error_rate_delta": (fault_c["error_rate"] - base_c["error_rate"])
                     if base_c and fault_c and base_c.get("n") and fault_c.get("n") else None},
        "removal_verified": removal, "recovery_verified": recovered,
        "recovery_time_s": rec.get("recovery_time_s"),
        "expected_workflows": expected, "surprises": surprises,
        "limitations": limitations, "facts": facts,
    }


def _normalise(detail: str) -> str:
    """Collapse per-request noise so identical failure modes group together."""
    import re
    d = re.sub(r"task[- ][0-9a-f-]+", "task <id>", detail)
    d = re.sub(r"\d+(\.\d+)?\s?(ms|s)\b", "<t>", d)
    return d[:140]
