"""Dependency map built from the compose file *and* the running containers.

Edge kinds:
  declared  - compose depends_on (with its condition)
  inferred  - a service's environment references another service's hostname
              (e.g. PAPERLESS_DBHOST=db, PAPERLESS_REDIS=redis://broker:6379)
  manual    - added by a user correction
User corrections can also suppress declared/inferred edges and annotate
nodes; they are stored separately and never overwrite discovery results.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Optional

import yaml

from . import config
from .docker_ops import ContainerInfo, Docker, DockerError

HOST_NODE = "host"


def load_compose(path: Path = config.COMPOSE_FILE) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _env_items(env) -> list[tuple[str, str]]:
    if isinstance(env, dict):
        return [(str(k), "" if v is None else str(v)) for k, v in env.items()]
    if isinstance(env, list):
        return [tuple(e.split("=", 1)) if "=" in e else (e, "") for e in env]  # type: ignore[misc]
    return []


def _refs(value: str, service: str) -> bool:
    """True when value is a hostname reference to service: 'db', 'db:5432',
    'redis://broker:6379', 'postgres://u:p@db/x'. Plain paths do not match."""
    v = value.strip()
    s = re.escape(service)
    return (v == service
            or re.fullmatch(rf"{s}:\d+", v) is not None
            or re.search(rf"(?://|@){s}(?::\d+)?(?:/|$)", v) is not None)


def _depends(spec) -> list[tuple[str, str]]:
    dep = spec.get("depends_on") or []
    if isinstance(dep, dict):
        return [(k, (v or {}).get("condition", "service_started")) for k, v in dep.items()]
    return [(d, "service_started") for d in dep]


def node_status(c: Optional[ContainerInfo]) -> str:
    if c is None:
        return "missing"
    if c.paused:
        return "paused"
    if not c.running:
        return c.state or "stopped"
    if c.health in ("unhealthy", "starting"):
        return c.health
    return "healthy" if c.health == "healthy" else "running"


def build_graph(compose: dict, containers: list[ContainerInfo] | None,
                overrides: dict | None = None, runtime_error: str | None = None) -> dict:
    services: dict = compose.get("services") or {}
    by_service = {c.service: c for c in (containers or []) if c.service}
    overrides = overrides or {}
    nodes, edges = [], []

    for name, spec in services.items():
        c = by_service.get(name)
        hc = spec.get("healthcheck")
        nodes.append({
            "id": name,
            "kind": "service",
            "image": spec.get("image", ""),
            "declared": True,
            "running": bool(c and c.running),
            "status": node_status(c) if containers is not None else "unknown",
            "health": c.health if c else None,
            "container": c.to_dict() if c else None,
            "healthcheck": " ".join(hc["test"]) if isinstance(hc, dict) and isinstance(hc.get("test"), list)
            else (hc or {}).get("test") if isinstance(hc, dict) else None,
            "ports": spec.get("ports") or [],
            "chaos_target": name in config.LIMITS.allowlist,
            "allowed_faults": sorted(config.LIMITS.allowlist.get(name, [])),
            "note": (overrides.get("notes") or {}).get(name, ""),
        })
        for dep, cond in _depends(spec):
            edges.append({"from": name, "to": dep, "kind": "declared",
                          "evidence": f"depends_on ({cond})"})
        env = _env_items(spec.get("environment"))
        if c:  # runtime env catches values from env_file / .env too
            env += _env_items(c.env)
        for other in services:
            if other == name:
                continue
            hits = sorted({k for k, v in env if _refs(v, other)})
            if hits:
                edges.append({"from": name, "to": other, "kind": "inferred",
                              "evidence": "env " + ", ".join(hits[:4])})
        if spec.get("ports"):
            edges.append({"from": HOST_NODE, "to": name, "kind": "inferred",
                          "evidence": "published port " + ", ".join(map(str, spec["ports"]))})

    # containers in the project that compose does not declare (e.g. sidecars)
    for c in containers or []:
        if c.service and c.service not in services:
            nodes.append({"id": c.service, "kind": "service", "declared": False, "image": c.image,
                          "running": c.running, "status": node_status(c), "health": c.health,
                          "container": c.to_dict(), "chaos_target": False, "allowed_faults": [],
                          "note": "running but not declared in compose"})

    if any(e["from"] == HOST_NODE for e in edges):
        nodes.insert(0, {"id": HOST_NODE, "kind": "external", "declared": False,
                         "status": "external", "image": "", "running": True,
                         "note": "chaos controller + synthetic workload (this machine)",
                         "chaos_target": False, "allowed_faults": []})

    # merge duplicate (from,to) pairs: declared + inferred become one edge with both kinds
    merged: dict[tuple[str, str], dict] = {}
    for e in edges:
        k = (e["from"], e["to"])
        if k in merged:
            merged[k]["kinds"].append(e["kind"])
            merged[k]["evidence"].append(e["evidence"])
        else:
            merged[k] = {"from": e["from"], "to": e["to"], "kinds": [e["kind"]],
                         "evidence": [e["evidence"]]}

    suppressed = []
    for r in overrides.get("remove_edges", []):
        k = (r["from"], r["to"])
        if k in merged:
            suppressed.append({**merged.pop(k), "note": r.get("note", "")})
    node_ids = {n["id"] for n in nodes}
    for a in overrides.get("add_edges", []):
        k = (a["from"], a["to"])
        if a["from"] in node_ids and a["to"] in node_ids:
            if k in merged:
                merged[k]["kinds"].append("manual")
                merged[k]["evidence"].append(f"manual: {a.get('note', '')}")
            else:
                merged[k] = {"from": a["from"], "to": a["to"], "kinds": ["manual"],
                             "evidence": [f"manual: {a.get('note', '')}"]}

    networks = sorted({n for c in containers or [] for n in c.networks})
    return {
        "generated": time.time(),
        "project": compose.get("name", config.COMPOSE_PROJECT),
        "source": {"compose_file": str(config.COMPOSE_FILE),
                   "runtime": "docker inspect" if containers is not None else None,
                   "runtime_error": runtime_error},
        "nodes": nodes,
        "edges": list(merged.values()),
        "suppressed_edges": suppressed,
        "networks": networks,
        "overrides": overrides,
    }


# ---- overrides persistence ------------------------------------------------------
def load_overrides(path: Path = config.GRAPH_OVERRIDES_FILE) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"add_edges": [], "remove_edges": [], "notes": {}}


def save_overrides(data: dict, path: Path = config.GRAPH_OVERRIDES_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def discover(docker: Docker, overrides_path: Path = config.GRAPH_OVERRIDES_FILE) -> dict:
    compose = load_compose()
    containers, err = None, None
    try:
        containers = docker.project_containers()
    except DockerError as e:
        err = str(e)
    return build_graph(compose, containers, load_overrides(overrides_path), err)
