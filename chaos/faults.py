"""Predefined, reversible fault injectors.

Each fault is fully described by a small JSON-serialisable *spec* so that the
out-of-process watchdog and crash recovery can rebuild it and remove it
without the original controller object.
"""
from __future__ import annotations

from typing import Optional

from . import config
from .docker_ops import Docker, DockerError
from .models import Check


class Fault:
    fault_type = ""

    def __init__(self, docker: Docker, spec: dict):
        self.docker = docker
        self.spec = spec
        self.container_id: str = spec["container_id"]
        self.service: str = spec["service"]
        self.run_id: str = spec["run_id"]

    def preflight(self) -> list[Check]:
        return []

    def inject(self) -> None:
        raise NotImplementedError

    def remove(self) -> None:
        """Idempotent: safe to call when the fault is not (or no longer) applied."""
        raise NotImplementedError

    def verify_removed(self) -> tuple[bool, str]:
        raise NotImplementedError


class PauseFault(Fault):
    """Freeze every process of the dependency container (SIGSTOP via the
    cgroup freezer). Connections stay open but nothing answers, which is what
    a hung database / broker looks like to Paperless."""

    fault_type = config.FAULT_PAUSE

    def preflight(self) -> list[Check]:
        info = self.docker.inspect(self.container_id)
        return [Check("target not already paused", not info.paused, info.state)]

    def inject(self) -> None:
        self.docker.pause(self.container_id)

    def remove(self) -> None:
        self.docker.unpause(self.container_id)

    def verify_removed(self) -> tuple[bool, str]:
        info = self.docker.inspect(self.container_id)
        ok = info.running and not info.paused
        return ok, f"state={info.state} paused={info.paused}"


class LatencyFault(Fault):
    """Add fixed egress delay on the dependency's eth0 using a sidecar that
    shares its network namespace. The sidecar removes the qdisc itself after
    the planned duration; remove() clears it immediately."""

    fault_type = config.FAULT_LATENCY

    def preflight(self) -> list[Check]:
        present = self.docker.image_present(config.NETEM_IMAGE)
        checks = [Check("netem helper image present", present,
                        config.NETEM_IMAGE if present else "run: python -m chaos up")]
        if present:
            try:
                out = self.docker.netem_show(self.container_id, self.run_id)
                checks.append(Check("no existing netem qdisc on target", "netem" not in out, out.strip()[:120]))
            except DockerError as e:
                checks.append(Check("tc available in target netns", False, str(e)))
        return checks

    def inject(self) -> None:
        self.docker.netem_start(self.container_id, self.run_id, self.spec["latency_ms"],
                                self.spec["duration_s"] + config.LIMITS.watchdog_grace_s)

    def remove(self) -> None:
        for sid in self.docker.netem_sidecars():
            self.docker.remove_netem_sidecar(sid)
        # killing the sidecar does not undo the qdisc (it lives in the target's netns)
        self.docker.netem_clear(self.container_id, self.run_id)

    def verify_removed(self) -> tuple[bool, str]:
        out = self.docker.netem_show(self.container_id, self.run_id + "-v")
        leftovers = self.docker.netem_sidecars()
        ok = "netem" not in out and not leftovers
        return ok, f"qdisc={out.strip()[:80]!r} sidecars={len(leftovers)}"


FAULTS: dict[str, type[Fault]] = {
    config.FAULT_PAUSE: PauseFault,
    config.FAULT_LATENCY: LatencyFault,
}


def build_fault(docker: Docker, spec: dict) -> Fault:
    cls = FAULTS.get(spec.get("fault_type", ""))
    if cls is None:
        raise ValueError(f"unknown fault type {spec.get('fault_type')!r}")
    return cls(docker, spec)


def emergency_rollback(docker: Docker, spec: Optional[dict] = None) -> tuple[bool, list[str]]:
    """Best-effort removal of *every* fault this tool can create, across all
    allowlisted sandbox services. Idempotent. Returns (verified, notes)."""
    notes: list[str] = []
    ok = True
    if spec:
        try:
            build_fault(docker, spec).remove()
            notes.append(f"removed {spec['fault_type']} on {spec['service']}")
        except Exception as e:  # keep going: the sweep below is the safety net
            notes.append(f"targeted removal failed: {e}")
    try:
        sidecars = docker.netem_sidecars()
        for sid in sidecars:
            docker.remove_netem_sidecar(sid)
        if sidecars:
            notes.append(f"removed {len(sidecars)} netem sidecar(s)")
    except Exception as e:
        ok = False
        notes.append(f"sidecar sweep failed: {e}")
    try:
        containers = docker.project_containers()
    except Exception as e:
        return False, notes + [f"cannot list sandbox containers: {e}"]
    netem_ready = _safe(lambda: docker.image_present(config.NETEM_IMAGE), False)
    for c in containers:
        if not c.sandbox or c.service not in config.LIMITS.allowlist:
            continue
        try:
            if c.paused:
                docker.unpause(c.id)
                notes.append(f"unpaused {c.service}")
            if netem_ready and c.running:
                if "netem" in docker.netem_show(c.id, "sweep"):
                    docker.netem_clear(c.id, "sweep")
                    notes.append(f"cleared netem on {c.service}")
                if "netem" in docker.netem_show(c.id, "sweep-v"):
                    ok = False
                    notes.append(f"netem still present on {c.service}")
            if docker.inspect(c.id).paused:
                ok = False
                notes.append(f"{c.service} still paused")
        except Exception as e:
            ok = False
            notes.append(f"{c.service}: {e}")
    return ok, notes


def _safe(fn, default):
    try:
        return fn()
    except Exception:
        return default
