"""Plain-text experiment report in the testing-chaos skill's output format."""
from __future__ import annotations

from datetime import datetime, timezone


def _ts(v) -> str:
    return datetime.fromtimestamp(v, timezone.utc).strftime("%H:%M:%S UTC") if v else "-"


def _pct(v) -> str:
    return "n/a" if v is None else f"{v:.0%}"


def _ms(v) -> str:
    return "n/a" if v is None else f"{v:.0f}ms"


def format_report(run: dict) -> str:
    cfg = run.get("config") or {}
    out = run.get("outcome") or {}
    metrics = (run.get("summary") or {}).get("metrics", {})
    rec = out.get("recovery") or {}
    rem = out.get("fault_removal") or {}
    impact = out.get("impact") or {}
    mark = lambda ok: "✅" if ok else ("❌" if ok is False else "–")  # noqa: E731
    lines = [
        "Chaos Test Results:",
        "─" * 30,
        f"Experiment: {cfg.get('name')} ({run['id']})",
        f"Hypothesis: {cfg.get('hypothesis') or '(none given)'}",
        f"Blast Radius: compose project paperless-chaos, service '{cfg.get('target')}' only",
        f"Fault: {cfg.get('fault_type')}"
        + (f" {cfg.get('latency_ms')}ms" if cfg.get("latency_ms") else "")
        + f", planned {cfg.get('duration_s')}s",
        f"State: {run['state']}  Result: {out.get('result')}",
        "",
        "Experiment Execution:",
        f"  1. Preflight checks: {sum(1 for c in out.get('preflight', []) if c['ok'])}/{len(out.get('preflight', []))} passed",
        f"  2. Baseline metrics collected ({(metrics.get('baseline') or {}).get('critical', {}).get('n', 0)} probes)",
        f"  3. Fault injected {_ts(out.get('fault_start'))}, removed {_ts(out.get('fault_end'))}",
        f"  4. Abort: {out.get('abort_reason') or 'none (ran planned duration)'}",
        "",
        "Results:",
        f"  {mark(impact.get('impact_detected'))} Application impact detected: {impact.get('impact_detected')}",
        f"  {mark(rem.get('verified'))} Fault removal verified: {rem.get('verified')}",
        f"  {mark(rec.get('verified'))} Recovery verified: {rec.get('verified')}"
        + (f" in {rec['recovery_time_s']:.1f}s" if rec.get("recovery_time_s") is not None else ""),
        "",
        "System Behavior Under Failure:",
    ]
    for phase in ("baseline", "fault", "recovery", "post"):
        c = (metrics.get(phase) or {}).get("critical")
        if c and c.get("n"):
            lines.append(f"  - {phase:<9} n={c['n']:<4} error rate {_pct(c['error_rate']):>5}  "
                         f"p50 {_ms(c['p50_ms']):>7}  p95 {_ms(c['p95_ms']):>7}")
    for o in impact.get("observations", []):
        lines.append(f"  - {o}")
    lines += ["", "Safety Controls:"]
    for b in out.get("breaches") or []:
        lines.append(f"  - Threshold breach: {b['message']}")
    lines.append("  - Automatic abort on error-rate / p95 thresholds; manual kill switch available")
    lines.append("  - Independent watchdog process armed for the fault deadline")
    for n in rem.get("notes", [])[:4]:
        lines.append(f"  - rollback: {n}")
    if rec.get("notes"):
        lines.append(f"  - recovery: {rec['notes']}")
    return "\n".join(lines)


# ---------------------------------------------------------------- Day 2: findings
def _score(v) -> str:
    return "n/a" if v is None else f"{v:.0f}/100"


def _signed(v) -> str:
    return "n/a" if v is None else f"{v:+}"


def format_analysis(f: dict) -> str:
    """Analyst finding for one run, appended to the run report."""
    d, det, sev, res = f["diagnosis"], f["deterministic"], f["severity"], f["resilience"]
    src = f["source"]
    who = (f"{src['provider']}:{src['model']}" if src["mode"] == "llm"
           else "deterministic rules (no LLM)" + (f"; LLM failed: {src['error']}" if src.get("error") else ""))
    facts = {x["id"]: x["text"] for x in det["facts"]}
    lines = [
        "", "Analysis:", "─" * 30,
        f"Impact (measured): {det['impact'].upper()} - {det['impact_reason']}",
        f"  failed: {', '.join(det['failed']) or 'none'} | degraded: {', '.join(det['degraded']) or 'none'}"
        f" | functional: {', '.join(det['functional']) or 'none'}",
        f"Severity (computed): {_score(sev['score'])} {sev['category']}"
        f"   Resilience (computed): {_score(res['score'])}",
        f"Diagnosis by: {who}",
        f"Summary: {d['summary']}",
    ]
    if d["inconclusive"]:
        lines.append("Result: INCONCLUSIVE - the evidence does not support a diagnosis.")
    for rc in d["root_causes"]:
        lines.append(f"Hypothesis {rc['rank']} [{rc['confidence']} confidence, {rc['component']}]: {rc['hypothesis']}")
        lines += [f"    + {i}: {facts.get(i, '?')}" for i in rc["supporting_evidence"]]
        lines += [f"    - {i}: {facts.get(i, '?')}" for i in rc["contradicting_evidence"]]
    if d["weakness"]:
        lines.append(f"Likely weakness: {d['weakness']}")
    r = d["recommendation"]
    lines += [f"Recommended fix [{r['category']}]: {r['action']}", f"  why: {r['rationale']}"]
    lines += [f"  verify: {s}" for s in d["verification"]["steps"]]
    lines += [f"  success when: {s}" for s in d["verification"]["success_criteria"]]
    lines.append(f"Confidence: {d['confidence']}")
    lines += [f"  limitation: {x}" for x in d["limitations"]]
    if f.get("retest"):
        t = f["retest"]
        lines.append(f"Retest vs {t['previous_run']}: impact {t['impact_before']} -> {t['impact_after']}, "
                     f"severity {_signed(t['severity_delta'])}, resilience {_signed(t['resilience_delta'])}")
    return "\n".join(lines)


def format_findings_report(plan: dict | None, findings: list[dict], overall: dict, runs: dict[str, dict],
                           graph: dict | None = None) -> str:
    """Final report across all analysed experiments. `findings` are ranked
    worst-first; `runs` maps run id -> stored run."""
    from . import scoring
    L = ["Paperless-ngx Chaos Lab - Findings Report",
         "=" * 42,
         f"Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
         "Measured values come from the experiment database. Text marked [AI] or [rules] is a",
         "hypothesis or recommendation derived from those measurements, not an observation.",
         "", "1. Overall resilience", "-" * 22,
         f"Score: {_score(overall['score'])}"
         + ("  (CAPPED: an experiment left recovery or rollback unverified)" if overall["capped_unrecovered"] else ""),
         f"Coverage: {overall['scenarios_scored']} of {overall['scenarios_total']} catalogue scenarios scored; "
         f"{overall['experiments_analysed']} experiments analysed "
         f"({', '.join(f'{v} {k}' for k, v in overall['counts'].items())})"]
    for k, v in overall["components"].items():
        L.append(f"  {k:<13} {'n/a' if v is None else f'{v:.0%}'}  (weight {overall['weights'][k]:.0%})")
    if overall["untested_scenarios"]:
        L.append("Not yet tested: " + ", ".join(f"{f} on {t}" for f, t in overall["untested_scenarios"]))
    L.append(overall["note"])

    if graph:
        L += ["", "2. Discovered dependencies", "-" * 27]
        L += [f"  {e['from']} -> {e['to']}  [{'+'.join(e['kinds'])}]  {'; '.join(e['evidence'])[:110]}"
              for e in graph.get("edges", [])] or ["  (none discovered)"]
        if (graph.get("source") or {}).get("runtime_error"):
            L.append(f"  runtime state unavailable: {graph['source']['runtime_error']}")

    L += ["", "3. Planned experiments (Agent 1 - Chaos Planner)", "-" * 49]
    if plan:
        src = plan["source"]
        L.append(f"Plan {plan['id']} by "
                 + (f"[AI] {src['provider']}:{src['model']}" if src["mode"] == "llm" else "[rules] deterministic ranking")
                 + (f" (LLM failed: {src['error']})" if src.get("error") and src["mode"] != "llm" else ""))
        for r in plan["recommendations"]:
            lat = f" {r['latency_ms']}ms" if r.get("latency_ms") else ""
            L.append(f"  #{r['rank']} {r['fault_type']}{lat} on {r['target']} for {r['duration_s']}s"
                     f"  -> {r['status']}" + (f" as {r['run_id']}" if r.get("run_id") else ""))
            L.append(f"      hypothesis: {r['hypothesis']}")
            L.append(f"      why: {r['rationale']}")
        for r in plan.get("rejected") or []:
            L.append(f"  rejected by validation: {r['name']}: {'; '.join(r['errors'])[:200]}")
        ex = plan.get("execution")
        if ex:
            L.append(f"Sequence {ex['status']}" + (f": {ex['stop_reason']}" if ex.get("stop_reason") else ""))
    else:
        L.append("No plan has been generated.")

    L += ["", "4. Executed experiments (measured)", "-" * 35]
    if not findings:
        L.append("No experiment has been analysed yet.")
    for f in sorted(findings, key=lambda x: x.get("run_created") or 0):
        det, run = f["deterministic"], runs.get(f["run_id"]) or {}
        c = det["critical"]
        lat = f" {det['latency_ms']}ms" if det.get("latency_ms") else ""
        L.append(f"  {f['run_id']}: {det['fault_type']}{lat} on {det['target']} - {det['result']} "
                 f"[{scoring.status_of(det)}]")
        if c["baseline"] and c["fault"] and c["fault"].get("n"):
            L.append(f"      critical probes: errors {_pct(c['baseline']['error_rate'])} -> "
                     f"{_pct(c['fault']['error_rate'])}, p95 {_ms(c['baseline']['p95_ms'])} -> "
                     f"{_ms(c['fault']['p95_ms'])}; fault active "
                     + ("n/a" if det["fault_duration_s"] is None else f"{det['fault_duration_s']:.1f}s")
                     + f" of {det['planned_duration_s']}s")
        else:
            L.append(f"      no comparable fault-phase metrics: {det['impact_reason']}")
        L.append(f"      impact {det['impact']}; rollback verified: {det['removal_verified']}; "
                 f"recovery verified: {det['recovery_verified']}"
                 + (f" in {det['recovery_time_s']:.1f}s" if det.get("recovery_time_s") is not None else ""))
        if (run.get("outcome") or {}).get("abort_reason"):
            L.append(f"      abort: {run['outcome']['abort_reason']}")

    L += ["", "5. Ranked weaknesses and recommended fixes", "-" * 43]
    # one entry per scenario: its most recent scored run (the one the overall score uses)
    chosen = {s["run_id"] for s in overall["per_scenario"]}
    newest: dict[tuple, dict] = {}
    older: dict[tuple, list[str]] = {}
    for f in sorted(findings, key=lambda x: -(x.get("run_created") or 0)):
        key = (f["deterministic"]["fault_type"], f["deterministic"]["target"])
        if f["severity"]["score"] is None:
            continue
        if f["run_id"] in chosen:
            newest[key] = f
        else:
            older.setdefault(key, []).append(f"{f['run_id']} ({_score(f['severity']['score'])})")
    n = 0
    for f in sorted(newest.values(), key=lambda x: -x["severity"]["score"]):
        d, det, sev = f["diagnosis"], f["deterministic"], f["severity"]
        n += 1
        tag = "[AI]" if f["source"]["mode"] == "llm" else "[rules]"
        facts = {x["id"]: x["text"] for x in det["facts"]}
        L.append(f"  {n}. severity {_score(sev['score'])} {sev['category'].upper()} - "
                 f"{det['fault_type']} on {det['target']} ({f['run_id']}), {det['impact']} impact")
        L.append(f"     measured: failed {det['failed'] or 'none'}, degraded {det['degraded'] or 'none'}, "
                 f"functional {det['functional'] or 'none'}")
        if d["weakness"]:
            L.append(f"     {tag} weakness: {d['weakness']}")
        for rc in d["root_causes"]:
            L.append(f"     {tag} hypothesis {rc['rank']} ({rc['confidence']}): {rc['hypothesis']}")
            L += [f"         evidence {i}: {facts.get(i, '?')}" for i in rc["supporting_evidence"][:4]]
            L += [f"         against  {i}: {facts.get(i, '?')}" for i in rc["contradicting_evidence"][:3]]
        L.append(f"     {tag} fix [{d['recommendation']['category']}]: {d['recommendation']['action']}")
        L += [f"         success when: {s}" for s in d["verification"]["success_criteria"]]
        if f.get("retest"):
            t = f["retest"]
            L.append(f"     change since previous run {t['previous_run']}: severity "
                     f"{_signed(t['severity_delta'])}, resilience {_signed(t['resilience_delta'])}")
        prior = older.get((det["fault_type"], det["target"]))
        if prior:
            L.append(f"     other runs of this scenario: {', '.join(prior)}")
    if not n:
        L.append("  No scoreable finding yet.")
    inconclusive = [f for f in findings if f["severity"]["score"] is None]
    if inconclusive:
        L.append("  Inconclusive (not scored): "
                 + "; ".join(f"{f['run_id']} ({f['deterministic']['impact_reason']})" for f in inconclusive))

    L += ["", "6. Scoring methodology", "-" * 23]
    L += ["  " + ln for ln in (scoring.__doc__ or "").strip().splitlines()[2:]]
    L += ["", "7. Limitations", "-" * 15,
          "  - Evidence comes from a synthetic workload of five probes against a single-node sandbox.",
          "  - Faults are limited to pause and added latency on db and broker; other failure modes are untested.",
          "  - An automatic abort shortens the fault window, so slow-onset effects may be missed.",
          "  - Root causes are hypotheses: no code or configuration of Paperless-ngx was inspected or changed.",
          "  - Workflow-to-service expectations are assumptions from the architecture, used only to flag surprises."]
    seen: list[str] = []
    for f in newest.values():
        for x in f["deterministic"]["limitations"]:
            if x not in seen:
                seen.append(x)
    L += [f"  - {x}" for x in seen[:12]]
    return "\n".join(L)
