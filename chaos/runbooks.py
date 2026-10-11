"""Predetermined remediation runbooks for the self-healing agent.

The AI only names a failure class. This table, not the AI, decides what is
done about it, and the executor below can only perform these fixed actions
on allowlisted sandbox services through docker_ops (which re-checks the
sandbox labels on every call). There is no way for a model to supply a
command, a container or a parameter.
"""
from __future__ import annotations

import time

from . import config
from .docker_ops import Docker

RUNBOOKS: dict[str, dict] = {
    "db_unavailable": {
        "id": "RB-1", "title": "Bring the database container back (unpause or start)", "action": "restore_container",
        "service": "db",
        "why": "A frozen or stopped database answers nothing. Resuming or starting the container restores service; "
               "its data lives on a volume.",
        "checks": ["database container running and not paused", "application requests succeed again"],
    },
    "broker_unavailable": {
        "id": "RB-2", "title": "Bring the message broker container back (unpause or start)",
        "action": "restore_container", "service": "broker",
        "why": "A frozen or stopped broker blocks task enqueueing and status checks. Bringing it back restores "
               "the queue.",
        "checks": ["broker container running and not paused", "application requests succeed again"],
    },
    "db_slow": {
        "id": "RB-3", "title": "Remove network impairment (delay / loss) from the database", "action": "clear_shaping",
        "service": "db",
        "why": "The database is running but its replies are delayed or dropped on the network path. Clearing "
               "the traffic-control rule on its interface restores normal round trips.",
        "checks": ["no traffic-control rule on the database interface", "application requests succeed again"],
    },
    "broker_slow": {
        "id": "RB-4", "title": "Remove network impairment (delay / loss) from the message broker",
        "action": "clear_shaping", "service": "broker",
        "why": "The broker is running but its replies are delayed or dropped on the network path. Clearing "
               "the traffic-control rule on its interface restores normal round trips.",
        "checks": ["no traffic-control rule on the broker interface", "application requests succeed again"],
    },
    "db_cpu_starved": {
        "id": "RB-5", "title": "Lift the CPU quota on the database", "action": "restore_cpu", "service": "db",
        "why": "The database is pinned at a CPU quota far below what it needs. Removing the quota lets it serve "
               "queries at normal speed.",
        "checks": ["no CPU quota on the database container", "application requests succeed again"],
    },
    "broker_cpu_starved": {
        "id": "RB-6", "title": "Lift the CPU quota on the message broker", "action": "restore_cpu",
        "service": "broker",
        "why": "The broker is pinned at a CPU quota far below what it needs. Removing the quota restores the queue.",
        "checks": ["no CPU quota on the broker container", "application requests succeed again"],
    },
    "db_refusing_connections": {
        "id": "RB-7", "title": "Remove the rule rejecting connections to the database", "action": "clear_firewall",
        "service": "db",
        "why": "The database is up but every connection to it is rejected on the network path. Removing the "
               "reject rule lets clients connect again.",
        "checks": ["no connection-reject rule on the database", "application requests succeed again"],
    },
    "broker_refusing_connections": {
        "id": "RB-8", "title": "Remove the rule rejecting connections to the message broker",
        "action": "clear_firewall", "service": "broker",
        "why": "The broker is up but every connection to it is rejected. Removing the reject rule restores the queue.",
        "checks": ["no connection-reject rule on the broker", "application requests succeed again"],
    },
    "db_memory_exhausted": {
        "id": "RB-9", "title": "Recreate the database with its declared memory settings", "action": "recreate",
        "service": "db",
        "why": "The database is squeezed by a memory limit and its processes are being killed. Recreating the "
               "container from its compose definition removes the limit; the data is on a volume.",
        "checks": ["no memory limit on the database container", "application requests succeed again"],
    },
    "broker_memory_exhausted": {
        "id": "RB-10", "title": "Recreate the message broker with its declared memory settings", "action": "recreate",
        "service": "broker",
        "why": "The broker is squeezed by a memory limit and keeps getting killed. Recreating it from its compose "
               "definition removes the limit; its data is on a volume.",
        "checks": ["no memory limit on the broker container", "application requests succeed again"],
    },
    "webserver_disk_full": {
        "id": "RB-11", "title": "Free space on the full upload scratch disk", "action": "clear_disk",
        "service": "webserver",
        "why": "Uploads are written to the scratch disk first; while it is full every upload fails. Deleting the "
               "file that filled it frees the space.",
        "checks": ["upload scratch disk below 90% used", "application requests succeed again"],
    },
    "unknown": {
        "id": "RB-0", "title": "Escalate to a human operator", "action": None, "service": None,
        "why": "The signals do not identify one dependency. No automatic action is safe, so nothing is changed.",
        "checks": [],
    },
}


def runbook_for(failure_class: str) -> dict:
    """The fixed rule: failure class -> runbook. Unknown classes escalate."""
    return {"failure_class": failure_class, **RUNBOOKS.get(failure_class, RUNBOOKS["unknown"])}


# which injectable faults each action undoes: an action may only run on a service
# where the safety allowlist permits one of those faults
ACTION_FAULTS = {
    "restore_container": {config.FAULT_PAUSE, config.FAULT_STOP},
    "clear_shaping": {config.FAULT_LATENCY, config.FAULT_LOSS},
    "restore_cpu": {config.FAULT_CPU},
    "clear_firewall": {config.FAULT_REFUSE},
    "recreate": {config.FAULT_MEMORY},
    "clear_disk": {config.FAULT_DISK},
}


def _allowed(service: str | None, action: str | None = None) -> bool:
    permitted = config.LIMITS.allowlist.get(service or "")
    return permitted is not None and bool(permitted & ACTION_FAULTS.get(action or "", set()))


def execute(docker: Docker, rb: dict) -> dict:
    """Run one runbook. Never raises; every step is recorded."""
    started = time.time()
    steps: list[dict] = []

    def step(name: str, ok: bool, detail: str) -> None:
        steps.append({"step": name, "ok": ok, "detail": detail, "ts": time.time()})

    if not rb.get("action"):
        step("no action", True, "escalated to a human; nothing was changed")
        return {"ok": False, "escalated": True, "steps": steps, "started": started, "ended": time.time()}
    svc = rb["service"]
    if not _allowed(svc, rb["action"]):
        step("policy check", False, f"service '{svc}' is not an approved sandbox target")
        return {"ok": False, "steps": steps, "started": started, "ended": time.time()}
    try:
        info = docker.resolve_service(svc)
        step("locate container", True, f"{info.name} ({info.id[:12]}) state={info.state} paused={info.paused}")
        if rb["action"] == "restore_container":
            if info.paused:
                docker.unpause(info.id)
                verb = "docker unpause"
            elif not info.running:
                docker.start(info.id)
                verb = "docker start"
            else:
                verb = "already running (no action needed)"
            after = docker.inspect(info.id)
            step(verb, after.running and not after.paused, f"after: state={after.state} paused={after.paused}")
        elif rb["action"] == "clear_firewall":
            for sid in docker.netem_sidecars_for(info.id):
                docker.remove_netem_sidecar(sid)
            docker.firewall_clear(info.id, "heal")
            step("remove connection-reject rule (iptables)", True, "reject rule removed")
        elif rb["action"] == "recreate":
            docker.recreate_service(svc)
            after = docker.resolve_service(svc)
            step("recreate from compose definition", after.memory == 0 and after.running,
                 f"after: memory limit={after.memory} state={after.state}")
        elif rb["action"] == "clear_disk":
            for sid in docker.disk_sidecars():
                docker.remove_netem_sidecar(sid)
            docker.disk_clear("heal")
            use = docker.disk_usage("heal-v")
            step("delete the file filling the scratch disk", use["used_pct"] < 90, f"after: {use['used_pct']}% used")
        elif rb["action"] == "restore_cpu":
            docker.set_cpus(info.id, 0)
            after = docker.inspect(info.id)
            step("docker update --cpu-quota -1 (remove quota)", after.cpu_limit == 0,
                 f"after: cpu_limit={after.cpu_limit:g}")
        elif rb["action"] == "clear_shaping":
            sidecars = docker.netem_sidecars_for(info.id)
            for sid in sidecars:
                docker.remove_netem_sidecar(sid)
            step("remove traffic-shaping sidecars", True, f"{len(sidecars)} removed")
            # the verify step checks the qdisc afterwards; checking here too would
            # cost another helper container on the critical path
            docker.netem_clear(info.id, "heal")
            step("tc qdisc del dev eth0 root", True, "delay removed from the interface")
        else:
            step("unknown action", False, str(rb["action"]))
    except Exception as e:  # noqa: BLE001 - a failed fix is a result, not a crash
        step("error", False, f"{type(e).__name__}: {e}"[:300])
    return {"ok": bool(steps) and all(s["ok"] for s in steps), "steps": steps,
            "started": started, "ended": time.time()}


def target_check(docker: Docker, rb: dict) -> tuple[bool, str]:
    """Is the condition this runbook fixes gone on its target?"""
    try:
        info = docker.resolve_service(rb["service"])
        if rb["action"] == "restore_container":
            ok = info.running and not info.paused and info.health in ("healthy", "none")
            return ok, f"{rb['service']}: state={info.state} paused={info.paused} health={info.health}"
        if rb["action"] == "clear_firewall":
            out = docker.firewall_show(info.id, "heal-c")
            return "CHAOS" not in out, f"{rb['service']}: {'reject rule present' if 'CHAOS' in out else 'no reject rule'}"
        if rb["action"] == "recreate":
            return info.memory == 0 and info.running, f"{rb['service']}: memory limit={info.memory} state={info.state}"
        if rb["action"] == "clear_disk":
            use = docker.disk_usage("heal-c")
            return use["used_pct"] < 90, f"scratch disk {use['used_pct']}% used"
        if rb["action"] == "restore_cpu":
            return info.cpu_limit == 0, f"{rb['service']}: cpu_limit={info.cpu_limit:g}"
        out = docker.netem_show(info.id, "heal-c")
        return "netem" not in out, f"{rb['service']}: qdisc {out.strip()[:80] or 'none'}"
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"[:200]
