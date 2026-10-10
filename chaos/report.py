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
