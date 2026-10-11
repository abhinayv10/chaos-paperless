"""Deterministic anomaly detection for the self-healing agent.

The agent is blind: it never sees which fault was injected, on which
service, or when. All it gets is what a monitoring system would see: the
workload probes (by timestamp only, never by experiment phase) and the
container states. From those it learns a healthy reference, then turns
every deviation into a numbered signal:

  A1, A2, ...  anomalies  (metric, normal value, observed value, the rule that fired)
  N1, N2, ...  normals    (what is still healthy; just as important for diagnosis)

The diagnosis step (AI or rules) may only reason from these ids, so every
conclusion on the dashboard traces back to a measured number.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Iterable, Optional

from .safety import percentile

PROBES = ("web_root", "api_documents", "api_status", "doc_upload")
SERVICES = ("db", "broker")
WINDOW_S = 15.0
ERR_MIN = 0.5          # error rate in the window must be at least this ...
ERR_RISE = 0.4         # ... and this far above the learned reference
MIN_FAILURES = 2       # failed requests needed in the window (one-off blips are ignored)
LAT_RATIO = 2.5        # p95 must be this many times the reference p95 ...
LAT_MIN_DELTA_MS = 800  # ... and at least this much slower
CONFIRM_TICKS = 2      # an anomaly must be present on consecutive checks to open an incident

# Failure taxonomy the diagnosis must choose from. The runbook table in
# runbooks.py maps each class to a predetermined fix.
FAILURE_CLASSES = {
    "db_unavailable": "Database not responding (container frozen, hung or stopped)",
    "db_slow": "Database network degraded (container running, replies delayed or lost)",
    "db_cpu_starved": "Database starved of CPU (container running at its CPU quota)",
    "broker_unavailable": "Message broker not responding (container frozen, hung or stopped)",
    "broker_slow": "Message broker network degraded (container running, replies delayed or lost)",
    "broker_cpu_starved": "Message broker starved of CPU (container running at its CPU quota)",
    "db_refusing_connections": "Database refusing connections (container up, connections rejected at once)",
    "broker_refusing_connections": "Message broker refusing connections (container up, connections rejected at once)",
    "db_memory_exhausted": "Database out of memory (squeezed by a memory limit, processes killed)",
    "broker_memory_exhausted": "Message broker out of memory (squeezed by a memory limit, processes killed)",
    "webserver_disk_full": "Web application's scratch disk is full (uploads cannot be written)",
    "unknown": "Cannot be attributed to one dependency from the available signals",
}
CPU_SATURATION = 0.7   # usage at or above this share of the container's CPU quota
MEM_SATURATION = 85.0  # % of a memory limit
DISK_FULL = 95.0       # % of the scratch volume
FAST_FAIL_MS = 2000    # failures quicker than this were refused/errored, not hung

# Which services each probe needs (from the Paperless architecture). The
# read path never touches the broker, so it separates db from broker faults.
PROBE_DEPENDS = {
    "web_root": ["db"],
    "api_documents": ["db"],
    "api_status": ["db", "broker"],
    "doc_upload": ["db", "broker"],
}
PROBE_LABEL = {
    "web_root": "Open the web UI", "api_documents": "List documents",
    "api_status": "System status check", "doc_upload": "Upload a document",
}


def expected_class(fault_type: Optional[str], target: Optional[str]) -> Optional[str]:
    """Ground truth for grading, used only after the incident, never by the agent."""
    if fault_type == "disk_exhaustion" and target == "webserver":
        return "webserver_disk_full"
    if target not in SERVICES or not fault_type:
        return None
    kind = {"dependency_pause": "unavailable", "dependency_stop": "unavailable", "network_latency": "slow",
            "packet_loss": "slow", "cpu_exhaustion": "cpu_starved", "connection_refused": "refusing_connections",
            "memory_exhaustion": "memory_exhausted"}.get(fault_type)
    return f"{target}_{kind}" if kind else None


def _stats(rows: list[dict]) -> dict:
    lat = [r["latency_ms"] or 0.0 for r in rows]
    ok_lat = [r["latency_ms"] or 0.0 for r in rows if r["ok"]]
    return {"n": len(rows), "failures": sum(1 for r in rows if not r["ok"]),
            "error_rate": (sum(1 for r in rows if not r["ok"]) / len(rows)) if rows else None,
            "p50_ms": percentile(lat, 50), "p95_ms": percentile(lat, 95),
            # slowness is judged on requests that succeeded: a timeout is an error, not a latency
            "ok_n": len(ok_lat), "ok_p95_ms": percentile(ok_lat, 95)}


def learn_reference(samples: Iterable[dict]) -> dict:
    """Healthy per-probe reference from the learning period."""
    by_probe: dict[str, list[dict]] = {p: [] for p in PROBES}
    for s in samples:
        if s["probe"] in by_probe:
            by_probe[s["probe"]].append(s)
    return {p: _stats(rows) for p, rows in by_probe.items() if rows}


def _pct(v: Optional[float]) -> str:
    return "n/a" if v is None else f"{v:.0%}"


def _ms(v: Optional[float]) -> str:
    return "n/a" if v is None else (f"{v / 1000:.1f}s" if v >= 1000 else f"{v:.0f}ms")


def _failure_mode(rows: list[dict]) -> str:
    details = Counter(re.sub(r"\d+(\.\d+)?\s?(ms|s)\b", "<t>", (r.get("detail") or "")[:120])
                      for r in rows if not r["ok"])
    return details.most_common(1)[0][0] if details else ""


_STATUS_RE = re.compile(r"db=(\S+) redis=(\S+)")


def detect(samples: Iterable[dict], health: Iterable[dict], reference: dict, now: float,
           window_s: float = WINDOW_S, resources: Optional[dict] = None) -> dict:
    """One check over the last window_s seconds. Pure function.

    samples / health: rows from storage (only 'ts', 'probe', 'ok',
    'latency_ms', 'detail' and 'service', 'state', 'health' are read; the
    experiment phase label is deliberately ignored)."""
    since = now - window_s
    window = [s for s in samples if s["ts"] >= since and s["probe"] in PROBES]
    anomalies: list[dict] = []
    normals: list[dict] = []

    def add(bucket: list, prefix: str, **kw) -> None:
        bucket.append({"id": f"{prefix}{len(bucket) + 1}", **kw})

    for p in PROBES:
        rows = [s for s in window if s["probe"] == p]
        if not rows:
            continue
        cur, ref = _stats(rows), reference.get(p) or {}
        ref_err = ref.get("error_rate") or 0.0
        flagged = False
        if (cur["failures"] >= MIN_FAILURES and cur["error_rate"] >= ERR_MIN
                and cur["error_rate"] - ref_err >= ERR_RISE):
            mode = _failure_mode(rows)
            fails = [r["latency_ms"] or 0.0 for r in rows if not r["ok"]]
            fail_p50 = percentile(fails, 50)
            fast = fail_p50 is not None and fail_p50 < FAST_FAIL_MS
            add(anomalies, "A", kind="error_rate", subject=p, metric="error rate",
                baseline=_pct(ref_err), observed=_pct(cur["error_rate"]),
                deviation=f"+{(cur['error_rate'] - ref_err) * 100:.0f} points",
                rule=f"error rate >= {ERR_MIN:.0%} and >= {ERR_RISE * 100:.0f} points above normal, "
                     f">= {MIN_FAILURES} failures",
                n=cur["n"], depends_on=PROBE_DEPENDS[p], fast_fail=fast, fail_p50_ms=fail_p50,
                signal=f"{PROBE_LABEL[p]} ({p}) failing: {cur['failures']}/{cur['n']} requests failed in the "
                       f"last {window_s:.0f}s (normal {_pct(ref_err)}); failures take {_ms(fail_p50)} "
                       + ("(fast: rejected or erroring, not hanging)" if fast else "(slow: hanging until a timeout)")
                       + (f"; typical error: {mode[:80]}" if mode else ""))
            flagged = True
        ref_p95, cur_p95 = ref.get("ok_p95_ms") or ref.get("p95_ms"), cur["ok_p95_ms"]
        if (ref_p95 and cur_p95 is not None and cur["ok_n"] >= 2
                and cur_p95 >= LAT_RATIO * ref_p95 and cur_p95 - ref_p95 >= LAT_MIN_DELTA_MS):
            add(anomalies, "A", kind="latency", subject=p, metric="p95 response time (successful requests)",
                baseline=_ms(ref_p95), observed=_ms(cur_p95),
                deviation=f"{cur_p95 / ref_p95:.1f}x",
                rule=f"p95 of successful requests >= {LAT_RATIO}x normal and >= {LAT_MIN_DELTA_MS}ms slower",
                n=cur["ok_n"], depends_on=PROBE_DEPENDS[p],
                signal=f"{PROBE_LABEL[p]} ({p}) slow: successful requests p95 {_ms(cur_p95)} vs normal "
                       f"{_ms(ref_p95)} ({cur_p95 / ref_p95:.1f}x)")
            flagged = True
        if p == "api_status":
            reports = [m for m in (_STATUS_RE.search(r.get("detail") or "") for r in rows) if m]
            if reports:
                db_s, redis_s = reports[-1].group(1), reports[-1].group(2)
                for comp, svc, val in (("database", "db", db_s), ("redis", "broker", redis_s)):
                    if val != "OK":
                        add(anomalies, "A", kind="dependency_report", subject=svc, metric=f"{comp} status",
                            baseline="OK", observed=val, deviation="not OK",
                            rule="Paperless' own status endpoint reports a dependency as not OK",
                            n=len(reports), depends_on=[svc],
                            signal=f"Paperless' status endpoint reports {comp} ({svc}) = {val}")
                        flagged = True
        if not flagged:
            add(normals, "N", kind="normal", subject=p, depends_on=PROBE_DEPENDS[p],
                signal=f"{PROBE_LABEL[p]} ({p}) normal: {cur['failures']}/{cur['n']} failed, p95 "
                       f"{_ms(cur['p95_ms'])} (normal {_pct(ref.get('error_rate'))} errors, "
                       f"p95 {_ms(ref_p95)})")

    latest: dict[str, dict] = {}
    for h in health:
        if h["ts"] >= since and h["service"] in SERVICES + ("webserver",):
            latest[h["service"]] = h
    for svc, h in sorted(latest.items()):
        state, hl = h.get("state"), h.get("health")
        res = (resources or {}).get(svc)
        if res and svc in SERVICES and res.get("memory_mb") and (res.get("mem_pct", 0) >= MEM_SATURATION
                                                              or res.get("oom_killed") or state != "running"):
            add(anomalies, "A", kind="memory_saturation", subject=svc, metric="memory use vs limit",
                baseline="no memory limit", observed=f"{res['memory_mb']:g} MB limit, {res.get('mem_pct', 0):.0f}% used",
                deviation="at its memory ceiling",
                rule=f"container has a memory limit and is >= {MEM_SATURATION:.0f}% of it, was OOM-killed or is restarting",
                n=1, depends_on=[svc],
                signal=f"Container '{svc}' is squeezed by a {res['memory_mb']:g} MB memory limit: "
                       f"{res.get('mem_pct', 0):.0f}% used, state {state}, OOM-killed: "
                       f"{'yes' if res.get('oom_killed') else 'no'}, restarts: {res.get('restart_count', 0)}")
            continue
        if res and svc == "webserver" and res.get("scratch_pct") is not None:
            if res["scratch_pct"] >= DISK_FULL:
                add(anomalies, "A", kind="disk_full", subject="webserver", metric="scratch disk use",
                    baseline="nearly empty", observed=f"{res['scratch_pct']:.0f}% of {res.get('scratch_mb', 0):g} MB",
                    deviation="full", rule=f"upload scratch volume >= {DISK_FULL:.0f}% used",
                    n=1, depends_on=["webserver"],
                    signal=f"The web application's upload scratch disk is {res['scratch_pct']:.0f}% full "
                           f"({res.get('scratch_mb', 0):g} MB volume)")
            else:
                add(normals, "N", kind="disk", subject="webserver", depends_on=["webserver"],
                    signal=f"The web application's upload scratch disk is {res['scratch_pct']:.0f}% used")
        if res and svc in SERVICES and state == "running" and res.get("cpu_limit"):
            share = res["cpu_pct"] / (res["cpu_limit"] * 100)
            if share >= CPU_SATURATION:
                add(anomalies, "A", kind="cpu_saturation", subject=svc, metric="CPU use vs quota",
                    baseline="no quota", observed=f"{res['cpu_limit']:g} CPU quota, {share:.0%} used",
                    deviation="at its CPU ceiling",
                    rule=f"container has a CPU quota and uses >= {CPU_SATURATION:.0%} of it",
                    n=1, depends_on=[svc],
                    signal=f"Container '{svc}' is pinned at its CPU quota: {res['cpu_pct']:.1f}% of a CPU used "
                           f"of a {res['cpu_limit']:g}-CPU quota ({share:.0%})")
                continue
        if state != "running" or hl == "unhealthy":
            add(anomalies, "A", kind="container_state", subject=svc, metric="container state",
                baseline="running", observed=f"{state}" + (f"/{hl}" if hl and hl != "none" else ""),
                deviation="not running normally", rule="container is not in the running state, or unhealthy",
                n=1, depends_on=[svc],
                signal=f"Container '{svc}' is {state}" + (f" (health: {hl})" if hl and hl != "none" else ""))
        else:
            cpu = ""
            if res and "cpu_pct" in res:
                cpu = f"; CPU {res['cpu_pct']:.1f}%" + (f" of a {res['cpu_limit']:g}-CPU quota"
                                                       if res.get("cpu_limit") else ", no quota")
                cpu += f"; memory {res.get('mem_pct', 0):.0f}%" + (f" of a {res['memory_mb']:g} MB limit"
                                                                   if res.get("memory_mb") else " (no limit)")
            add(normals, "N", kind="container", subject=svc, depends_on=[svc],
                signal=f"Container '{svc}' is running" + (f" and {hl}" if hl and hl != "none" else "") + cpu)
    return {"ts": now, "window_s": window_s, "anomalies": anomalies, "normals": normals,
            "samples_in_window": len(window)}


def signal_ids(snapshot: dict) -> set[str]:
    return {s["id"] for s in snapshot["anomalies"] + snapshot["normals"]}


# ---------------------------------------------------------------- rules classifier
def rules_diagnosis(snapshot: dict, excluded: Iterable[str] = ()) -> dict:
    """Deterministic diagnosis from the same signals the AI sees. Used as the
    fallback when no model is available and as a cross-check of the AI."""
    excluded = set(excluded)
    A, N = snapshot["anomalies"], snapshot["normals"]
    ids = lambda rows: [r["id"] for r in rows]  # noqa: E731
    frozen = [a for a in A if a["kind"] == "container_state" and a["subject"] in SERVICES]
    starved = [a for a in A if a["kind"] == "cpu_saturation"]
    squeezed = [a for a in A if a["kind"] == "memory_saturation"]
    disk = [a for a in A if a["kind"] == "disk_full"]
    fast_reads = [a for a in A if a["kind"] == "error_rate" and a["subject"] in ("web_root", "api_documents")
                  and a.get("fast_fail")]
    fast_broker = [a for a in A if a["kind"] == "error_rate" and a["subject"] in ("api_status", "doc_upload")
                   and a.get("fast_fail")]
    reads_bad = [a for a in A if a["subject"] in ("web_root", "api_documents")]
    reads_ok = [n for n in N if n["subject"] in ("web_root", "api_documents")]
    broker_path_bad = [a for a in A if a["subject"] in ("api_status", "doc_upload")]
    report_db = [a for a in A if a["kind"] == "dependency_report" and a["subject"] == "db"]
    report_broker = [a for a in A if a["kind"] == "dependency_report" and a["subject"] == "broker"]
    running = {n["subject"]: n for n in N if n["kind"] == "container"}
    steps: list[dict] = []
    if squeezed:
        svc = squeezed[0]["subject"]
        cls = f"{svc}_memory_exhausted"
        steps.append({"step": f"The {svc} container is pinned at a memory limit ({squeezed[0]['observed']}).",
                      "cites": ids(squeezed)})
        affected = [a for a in A if svc in a.get("depends_on", []) and a["kind"] != "memory_saturation"]
        if affected:
            steps.append({"step": f"Workflows that need {svc} are failing, consistent with its processes being "
                                  f"killed for lack of memory.", "cites": ids(affected)[:6]})
    elif frozen:
        svc = frozen[0]["subject"]
        cls = f"{svc}_unavailable"
        steps.append({"step": f"The {svc} container is not running normally ({frozen[0]['observed']}).",
                      "cites": ids(frozen)})
        affected = [a for a in A if svc in a.get("depends_on", []) and a["kind"] != "container_state"]
        if affected:
            steps.append({"step": f"Workflows that need {svc} are failing or slow, consistent with {svc} "
                                  f"not answering.", "cites": ids(affected)[:6]})
    elif starved:
        svc = starved[0]["subject"]
        cls = f"{svc}_cpu_starved"
        steps.append({"step": f"The {svc} container is running at its CPU ceiling, so it cannot keep up.",
                      "cites": ids(starved)})
        affected = [a for a in A if svc in a.get("depends_on", []) and a["kind"] != "cpu_saturation"]
        if affected:
            steps.append({"step": f"Workflows that need {svc} are failing or slow, consistent with a starved {svc}.",
                          "cites": ids(affected)[:6]})
    elif disk:
        cls = "webserver_disk_full"
        steps.append({"step": "The upload scratch disk of the web application is full.", "cites": ids(disk)})
        uploads = [a for a in A if a["subject"] == "doc_upload"]
        if uploads:
            steps.append({"step": "Uploads fail because they are written to that disk first.", "cites": ids(uploads)})
        if reads_ok:
            steps.append({"step": "Reads do not write to disk and are still normal.", "cites": ids(reads_ok)})
    elif fast_reads:
        cls = "db_refusing_connections"
        steps.append({"step": "The read path fails immediately rather than hanging: the database rejects "
                              "connections instead of being slow.", "cites": ids(fast_reads)[:6]})
    elif fast_broker and not reads_bad:
        cls = "broker_refusing_connections"
        steps.append({"step": "Only broker-dependent workflows fail, and they fail immediately: the broker rejects "
                              "connections.", "cites": ids(fast_broker)[:6]})
        if reads_ok:
            steps.append({"step": "Reads that need only the database are normal.", "cites": ids(reads_ok)})
    elif reads_bad or report_db:
        cls = "db_slow"
        steps.append({"step": "The read path (web UI, document list) only needs the database, and it is "
                              "failing or slow, so the database is the common dependency.",
                      "cites": ids(reads_bad + report_db)[:6]})
    elif broker_path_bad or report_broker:
        cls = "broker_slow"
        steps.append({"step": "Only workflows that also need the message broker are affected.",
                      "cites": ids(broker_path_bad + report_broker)[:6]})
        if reads_ok:
            steps.append({"step": "Reads that need only the database are still normal, which rules out "
                                  "the database.", "cites": ids(reads_ok)})
    else:
        cls = "unknown"
        steps.append({"step": "The anomalies do not point at one dependency.", "cites": ids(A)[:6]})
    if cls.endswith("_slow") or cls.endswith("_refusing_connections"):
        svc = cls.split("_")[0]
        if svc in running:
            steps.append({"step": f"The {svc} container is still running and not CPU-bound, so the problem is "
                                  f"on the network path rather than the container itself.",
                          "cites": [running[svc]["id"]]})
    if cls in excluded:
        steps.append({"step": f"The runbook for {cls} was already tried and the anomalies persisted; "
                              f"no other class is supported by these rules.", "cites": ids(A)[:3]})
        cls = "unknown"
    return {
        "failure_class": cls,
        "confidence": "medium" if cls != "unknown" else "low",
        "summary": FAILURE_CLASSES[cls] + ".",
        "reasoning": steps,
        "ruled_out": [],
    }
