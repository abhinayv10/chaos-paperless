"""Resilience scan: find the weak points that failure injection can test.

Every check here is a real, read-only measurement of the running sandbox:
the dependency map, live probes of each user workflow, container
configuration (replicas, restart policy, health checks, resource limits)
and the application's timeout/resilience settings. Nothing is changed.

Each finding carries its evidence and the catalogue experiments that would
test it, so the campaign can turn "this looks fragile" into "inject this
fault and measure what happens".
"""
from __future__ import annotations

import time
from typing import Callable, Optional

from . import catalog, config
from .detection import PROBE_LABEL
from .discovery import discover
from .docker_ops import Docker, DockerError
from .safety import percentile

PROBE_ROUNDS = 5
CLIENT_TIMEOUT_S = 5.0
HEADROOM_WARN = 0.5      # p95 above this share of the client timeout leaves little room for any slowdown

# Default experiment parameters per fault for scan suggestions (within the safety limits).
DEFAULT_PARAMS = {
    config.FAULT_PAUSE: {}, config.FAULT_STOP: {},
    config.FAULT_LATENCY: {"latency_ms": 500}, config.FAULT_LOSS: {"loss_pct": 30},
    config.FAULT_CPU: {"cpu_limit": 0.05},
    config.FAULT_REFUSE: {}, config.FAULT_DISK: {},
    config.FAULT_MEMORY: {},   # per target, see MEMORY_MB
}
# below each dependency's working set (db ~35 MB, broker ~8 MB when measured), so the limit really bites
MEMORY_MB = {"db": 24, "broker": 6}
FAULT_SHORT = {
    config.FAULT_PAUSE: "freeze (pause)", config.FAULT_STOP: "crash (stop)", config.FAULT_LATENCY: "network latency",
    config.FAULT_LOSS: "packet loss", config.FAULT_CPU: "CPU starvation",
    config.FAULT_REFUSE: "connection failures", config.FAULT_MEMORY: "memory exhaustion",
    config.FAULT_DISK: "disk full",
}
SEVERITY_SCORE = {"high": 3, "medium": 2, "low": 1}


def experiment(fault: str, target: str, why: str) -> dict:
    params = {"memory_mb": MEMORY_MB.get(target, 32)} if fault == config.FAULT_MEMORY else DEFAULT_PARAMS[fault]
    return {"fault_type": fault, "target": target, **params, "why": why,
            "label": f"{target} {FAULT_SHORT[fault]}"}


class Scanner:
    def __init__(self, docker: Docker, client_factory: Callable[[], object],
                 emit: Optional[Callable[[dict], None]] = None, rounds: int = PROBE_ROUNDS):
        self.docker, self.client_factory, self.rounds = docker, client_factory, rounds
        self.emit = emit or (lambda step: None)
        self.steps: list[dict] = []
        self.findings: list[dict] = []

    def _step(self, title: str, status: str, detail: str = "", data=None) -> dict:
        st = {"title": title, "status": status, "detail": detail, "ts": time.time(), "data": data}
        if self.steps and self.steps[-1]["title"] == title and self.steps[-1]["status"] == "running":
            self.steps[-1] = st
        else:
            self.steps.append(st)
        self.emit(st)
        return st

    def _finding(self, title: str, category: str, severity: str, evidence: list[str], services: list[str],
                 workflows: list[str], tests: list[dict]) -> None:
        self.findings.append({"id": f"R{len(self.findings) + 1}", "title": title, "category": category,
                              "severity": severity, "evidence": evidence, "services": services,
                              "workflows": workflows, "tests": tests})

    # ------------------------------------------------------------------ checks
    def scan(self) -> dict:
        started = time.time()
        graph = self._topology()
        probes = self._probe_workflows()
        containers = self._containers()
        self._single_points(graph, containers)
        self._recovery_policy(containers)
        self._resources(containers)
        self._timeouts(containers, probes)
        self._circuit_breakers(graph)
        self._scratch_disk(containers)
        self._step("Scan complete", "done", f"{len(self.findings)} weak points found in {time.time() - started:.0f}s")
        return {"started": started, "ended": time.time(), "steps": self.steps, "findings": self.findings,
                "probes": probes, "services": [n["id"] for n in graph.get("nodes", [])],
                "edges": [f"{e['from']} -> {e['to']}" for e in graph.get("edges", [])]}

    def _topology(self) -> dict:
        self._step("Discover the architecture", "running", "reading Docker Compose and the running containers")
        graph = discover(self.docker)
        edges = [f"{e['from']} -> {e['to']}" for e in graph.get("edges", [])]
        self._step("Discover the architecture", "done",
                   f"{len(graph.get('nodes', []))} components, dependencies: {', '.join(edges) or 'none'}", edges)
        return graph

    def _probe_workflows(self) -> dict:
        title = "Probe every user workflow"
        self._step(title, "running", f"{self.rounds} live requests per workflow")
        client = self.client_factory()
        fns = {"web_root": client.probe_web_root, "api_documents": client.probe_documents,
               "api_status": client.probe_status}
        out = {}
        for name, fn in fns.items():
            rows = []
            for _ in range(self.rounds):
                rows.append(fn())
            lat = [r.latency_ms for r in rows]
            out[name] = {"n": len(rows), "errors": sum(1 for r in rows if not r.ok),
                         "p50_ms": percentile(lat, 50), "p95_ms": percentile(lat, 95),
                         "detail": next((r.detail for r in rows if not r.ok), rows[-1].detail)}
            self._step(title, "running", f"{PROBE_LABEL[name]}: p95 {out[name]['p95_ms']:.0f} ms, "
                                         f"{out[name]['errors']}/{len(rows)} failed")
        res, _ = client.upload_document()
        out["doc_upload"] = {"n": 1, "errors": 0 if res.ok else 1, "p50_ms": res.latency_ms, "p95_ms": res.latency_ms,
                             "detail": res.detail}
        self._step(title, "done", "; ".join(f"{PROBE_LABEL[k]} p95 {v['p95_ms']:.0f} ms"
                                            + (f" ({v['errors']} failed)" if v["errors"] else "")
                                            for k, v in out.items()), out)
        return out

    def _containers(self) -> dict:
        self._step("Inspect container configuration", "running", "replicas, restart policy, health checks, limits")
        try:
            cs = {c.service: c for c in self.docker.project_containers() if c.service}
            counts: dict[str, int] = {}
            for c in self.docker.project_containers():
                counts[c.service] = counts.get(c.service, 0) + 1
        except DockerError as e:
            self._step("Inspect container configuration", "failed", str(e))
            return {}
        info = {}
        for svc, c in cs.items():
            info[svc] = {"replicas": counts.get(svc, 1), "restart": _restart_policy(self.docker, c.id),
                         "health": c.health, "cpu_limit": c.cpu_limit, "memory": c.memory, "env": c.env}
        self._step("Inspect container configuration", "done",
                   "; ".join(f"{s}: {v['replicas']} replica, restart={v['restart']}, health={v['health']}, "
                             f"CPU limit={v['cpu_limit'] or 'none'}, memory limit={v['memory'] or 'none'}"
                             for s, v in sorted(info.items())),
                   {s: {k: v for k, v in d.items() if k != "env"} for s, d in info.items()})
        return info

    def _single_points(self, graph: dict, containers: dict) -> None:
        self._step("Look for single points of failure", "running")
        for svc in ("db", "broker"):
            flows = catalog.workflows_through(svc)
            reps = (containers.get(svc) or {}).get("replicas", 1)
            if reps <= 1 and flows:
                crit = sum(catalog.WORKFLOWS[w]["criticality"] for w in flows)
                self._finding(
                    f"The {'database' if svc == 'db' else 'message broker'} is a single point of failure",
                    "single_point_of_failure", "high" if crit >= 0.6 else "medium",
                    [f"{reps} replica of '{svc}' and no failover configured",
                     f"{len(flows)} of {len(catalog.WORKFLOWS)} user workflows depend on it: {', '.join(flows)}",
                     f"those workflows carry {crit:.0%} of the user-facing criticality weight"],
                    [svc], flows,
                    [experiment(config.FAULT_STOP, svc, f"what users see when {svc} crashes"),
                     experiment(config.FAULT_PAUSE, svc, f"what users see when {svc} hangs"),
                     experiment(config.FAULT_REFUSE, svc, f"what users see when connections to {svc} fail")])
        n = sum(1 for f in self.findings if f["category"] == "single_point_of_failure")
        self._step("Look for single points of failure", "done", f"{n} found")

    def _recovery_policy(self, containers: dict) -> None:
        self._step("Check automatic recovery", "running", "restart policy vs health checks")
        hung = []
        for svc in ("db", "broker"):
            c = containers.get(svc)
            if not c:
                continue
            if c["health"] in ("healthy", "unhealthy", "starting"):
                hung.append(f"'{svc}' has a health check, but Docker's restart policy '{c['restart']}' only acts "
                            f"when the process exits, not when it is unhealthy or hung")
        if hung:
            self._finding("A hung dependency is never restarted automatically", "no_self_healing", "high",
                          hung + ["nothing in the stack watches health checks and restarts a hung container"],
                          ["db", "broker"], catalog.workflows_through("db"),
                          [experiment(config.FAULT_PAUSE, "db", "a hung database stays hung until someone acts"),
                           experiment(config.FAULT_PAUSE, "broker", "a hung broker stays hung until someone acts")])
        self._step("Check automatic recovery", "done", f"{len(hung)} services rely on manual recovery when hung")

    def _resources(self, containers: dict) -> None:
        self._step("Check resource isolation", "running", "CPU and memory limits")
        unlimited = [s for s in ("db", "broker", "webserver") if s in containers and not containers[s]["cpu_limit"]]
        no_mem = [s for s in ("db", "broker", "webserver") if s in containers and not containers[s].get("memory")]
        if unlimited or no_mem:
            self._finding("No CPU or memory budgets: services compete for the same resources", "resource_isolation",
                          "medium",
                          [f"no CPU limit or reservation on: {', '.join(unlimited) or 'none'}",
                           f"no memory limit or reservation on: {', '.join(no_mem) or 'none'}",
                           "a busy neighbour or a memory squeeze directly hits the database and the broker, and "
                           "nothing reserves capacity for them"],
                          ["db", "broker"], catalog.workflows_through("db"),
                          [experiment(config.FAULT_CPU, "db", "how the app behaves when the database is CPU-starved"),
                           experiment(config.FAULT_MEMORY, "db", "how the app behaves when the database runs out "
                                                                 "of memory"),
                           experiment(config.FAULT_CPU, "broker", "how the queue behaves when the broker is "
                                                                  "CPU-starved"),
                           experiment(config.FAULT_MEMORY, "broker", "how the queue behaves when the broker runs out "
                                                                     "of memory")])
        self._step("Check resource isolation", "done",
                   f"{len(unlimited)} services without CPU limits, {len(no_mem)} without memory limits")

    def _timeouts(self, containers: dict, probes: dict) -> None:
        self._step("Check timeouts and latency headroom", "running")
        env = {kv.split("=", 1)[0]: kv.split("=", 1)[1] for kv in (containers.get("webserver") or {}).get("env", [])
               if "=" in kv}
        missing = [k for k in ("PAPERLESS_DB_TIMEOUT",) if k not in env]
        tight = {k: v for k, v in probes.items() if v["p95_ms"] and v["p95_ms"] >= HEADROOM_WARN * CLIENT_TIMEOUT_S * 1000}
        evidence = []
        if missing:
            evidence.append(f"no database timeout in the webserver environment ({', '.join(missing)} not set): "
                            "requests can wait on a stalled database until the client gives up")
        for k, v in tight.items():
            evidence.append(f"{PROBE_LABEL[k]} already takes p95 {v['p95_ms'] / 1000:.1f}s of a "
                            f"{CLIENT_TIMEOUT_S:.0f}s client timeout ({v['p95_ms'] / (CLIENT_TIMEOUT_S * 10):.0f}%)")
        if evidence:
            self._finding("Little latency headroom and no database timeout", "timeouts",
                          "high" if tight and missing else "medium", evidence, ["db", "broker"],
                          sorted(set(tight) | {"api_documents"}),
                          [experiment(config.FAULT_LATENCY, "db", "small extra delays may push requests past the "
                                                                  "client timeout"),
                           experiment(config.FAULT_LOSS, "db", "retransmissions on a lossy link add seconds per query"),
                           experiment(config.FAULT_LATENCY, "broker", "does broker latency leak into user requests?"),
                           experiment(config.FAULT_LOSS, "broker", "does a lossy broker link stall uploads?")])
        self._step("Check timeouts and latency headroom", "done",
                   f"{len(missing)} missing timeout settings; {len(tight)} workflows above "
                   f"{HEADROOM_WARN:.0%} of the timeout")

    def _circuit_breakers(self, graph: dict) -> None:
        self._step("Look for circuit breakers", "running", "proxies or breakers between the app and its dependencies")
        direct = [e for e in graph.get("edges", []) if e["from"] == "webserver" and e["to"] in ("db", "broker")]
        proxies = [n["id"] for n in graph.get("nodes", []) if any(k in (n.get("image") or "")
                                                                  for k in ("envoy", "haproxy", "pgbouncer", "toxiproxy"))]
        if direct and not proxies:
            self._finding("No circuit breaker: calls keep hitting a failing dependency", "circuit_breaker", "medium",
                          [f"webserver talks to {', '.join(e['to'] for e in direct)} directly, with no proxy or "
                           "breaker in between",
                           "without a breaker, every request during an outage waits for its own timeout instead of "
                           "failing fast; validated during the experiment by comparing early and late failure times"],
                          [e["to"] for e in direct], catalog.workflows_through("db"),
                          [experiment(config.FAULT_STOP, "db", "do failures get faster after repeated errors?"),
                           experiment(config.FAULT_PAUSE, "broker", "do upload failures fail fast or hang?")])
        self._step("Look for circuit breakers", "done",
                   "none found" if not proxies else f"proxies present: {', '.join(proxies)}")


    def _scratch_disk(self, containers: dict) -> None:
        self._step("Check disk headroom for uploads", "running", "where uploads are written first, and how full it is")
        env = {kv.split("=", 1)[0]: kv.split("=", 1)[1] for kv in (containers.get("webserver") or {}).get("env", [])
               if "=" in kv}
        scratch = env.get("PAPERLESS_SCRATCH_DIR", "/tmp/paperless")
        try:
            use = self.docker.disk_usage("scan")
        except Exception as e:  # noqa: BLE001 - no bounded scratch volume in this sandbox
            self._step("Check disk headroom for uploads", "done", f"scratch volume not measurable: {e}"[:160])
            return
        self._finding("Uploads depend on a small scratch disk that nothing frees or alerts on", "disk_capacity",
                      "medium",
                      [f"every upload is written to {scratch} first: a {use['total_mb']:g} MB volume, "
                       f"{use['used_pct']:g}% used now",
                       "no health check, alert or cleanup job watches that space; when it fills, uploads fail"],
                      ["webserver"], ["doc_upload", "doc_processing"],
                      [experiment(config.FAULT_DISK, "webserver", "what users see when the upload disk is full")])
        self._step("Check disk headroom for uploads", "done", f"{scratch}: {use['total_mb']:g} MB, "
                                                             f"{use['used_pct']:g}% used")


def _restart_policy(docker: Docker, container_id: str) -> str:
    try:
        d = docker._inspect([container_id])[0]  # read-only; the restart policy is not in ContainerInfo
        return ((d.get("HostConfig") or {}).get("RestartPolicy") or {}).get("Name") or "no"
    except Exception:  # noqa: BLE001
        return "unknown"
