"""Predefined, reversible fault injectors.

Each fault is fully described by a small JSON-serialisable *spec* so that the
out-of-process watchdog and crash recovery can rebuild it and remove it
without the original controller object.
"""
from __future__ import annotations

import time
from typing import Optional

from . import config
from .docker_ops import Docker, DockerError, NotInSandbox
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
        self.docker.netem_start(self.container_id, self.run_id, self.spec.get("latency_ms"),
                                self.spec["duration_s"] + config.LIMITS.watchdog_grace_s,
                                loss_pct=self.spec.get("loss_pct"))

    def remove(self) -> None:
        for sid in self.docker.netem_sidecars():
            self.docker.remove_netem_sidecar(sid)
        # killing the sidecar does not undo the qdisc (it lives in the target's netns)
        self.docker.netem_clear(self.container_id, self.run_id)

    def verify_removed(self) -> tuple[bool, str]:
        out = self.docker.netem_show(self.container_id, self.run_id + "-v")
        leftovers = self.docker.netem_sidecars()
        # short-lived show/clear helpers (--rm) can still be listed for a moment
        for _ in range(3):
            if not leftovers or "netem" in out:
                break
            time.sleep(1.0)
            leftovers = self.docker.netem_sidecars()
        ok = "netem" not in out and not leftovers
        return ok, f"qdisc={out.strip()[:80]!r} sidecars={len(leftovers)}"


class PacketLossFault(LatencyFault):
    """Drop a percentage of the dependency's egress packets (tc netem loss).
    Same sidecar, removal and verification as the latency fault."""

    fault_type = config.FAULT_LOSS


class StopFault(Fault):
    """Stop the dependency container (docker stop). Unlike a pause, the process
    is gone: connections are closed and new ones are refused. The compose
    restart policy (unless-stopped) does not bring it back by itself."""

    fault_type = config.FAULT_STOP

    def preflight(self) -> list[Check]:
        info = self.docker.inspect(self.container_id)
        return [Check("target running", info.running and not info.paused, info.state)]

    def inject(self) -> None:
        self.docker.stop(self.container_id)

    def remove(self) -> None:
        self.docker.start(self.container_id)

    def verify_removed(self) -> tuple[bool, str]:
        info = self.docker.inspect(self.container_id)
        return info.running and not info.paused, f"state={info.state} health={info.health}"


class CpuFault(Fault):
    """Starve the dependency of CPU with a tight quota (docker update --cpus).
    The sandbox sets no CPU limits, so removal restores 'unlimited'."""

    fault_type = config.FAULT_CPU

    def preflight(self) -> list[Check]:
        info = self.docker.inspect(self.container_id)
        return [Check("target has no CPU quota to overwrite", info.cpu_limit == 0,
                      f"cpu_limit={info.cpu_limit:g}")]

    def inject(self) -> None:
        self.docker.set_cpus(self.container_id, self.spec["cpu_limit"])

    def remove(self) -> None:
        self.docker.set_cpus(self.container_id, 0)

    def verify_removed(self) -> tuple[bool, str]:
        info = self.docker.inspect(self.container_id)
        return info.cpu_limit == 0, f"cpu_limit={info.cpu_limit:g}"


class FirewallFault(Fault):
    """Reject every connection to the dependency's service port (iptables in its
    network namespace). The container keeps running and stays healthy from the
    inside, but every client gets an immediate connection reset or refusal."""

    fault_type = config.FAULT_REFUSE

    def preflight(self) -> list[Check]:
        present = self.docker.image_present(config.NETEM_IMAGE)
        checks = [Check("helper image present", present, config.NETEM_IMAGE if present else "run: python -m chaos up")]
        if present:
            try:
                out = self.docker.firewall_show(self.container_id, self.run_id)
                checks.append(Check("iptables available, no leftover rule", "CHAOS" not in out, out.strip()[:120]))
            except DockerError as e:
                checks.append(Check("iptables available in target netns", False, str(e)))
        return checks

    def inject(self) -> None:
        self.docker.firewall_start(self.container_id, self.run_id, config.SERVICE_PORTS[self.service],
                                   self.spec["duration_s"] + config.LIMITS.watchdog_grace_s)

    def remove(self) -> None:
        for sid in self.docker.netem_sidecars_for(self.container_id):
            self.docker.remove_netem_sidecar(sid)
        self.docker.firewall_clear(self.container_id, self.run_id)

    def verify_removed(self) -> tuple[bool, str]:
        out = self.docker.firewall_show(self.container_id, self.run_id + "-v")
        return "CHAOS" not in out, f"iptables rules: {'CHAOS chain present' if 'CHAOS' in out else 'clean'}"


class MemoryFault(Fault):
    """Squeeze the dependency into a memory limit below its working set
    (docker update --memory). Processes get OOM-killed and restarted. Docker
    cannot lift a memory limit with an update, so removal recreates the
    container from its compose definition (data is on a named volume). The
    container id changes, so removal and verification resolve the service."""

    fault_type = config.FAULT_MEMORY

    def preflight(self) -> list[Check]:
        info = self.docker.inspect(self.container_id)
        return [Check("target has no memory limit to overwrite", info.memory == 0, f"memory={info.memory}")]

    def inject(self) -> None:
        self.docker.set_memory(self.container_id, self.spec["memory_mb"])

    def _resolve(self):
        """While compose recreates the container, the service briefly has two."""
        for _ in range(30):
            try:
                return self.docker.resolve_service(self.service)
            except NotInSandbox as e:
                if "ambiguity" not in str(e):
                    raise
                time.sleep(2.0)
        return self.docker.resolve_service(self.service)

    def remove(self) -> None:
        if self._resolve().memory:
            self.docker.recreate_service(self.service)

    def verify_removed(self) -> tuple[bool, str]:
        info = self._resolve()
        return info.memory == 0 and info.running, f"memory={info.memory} state={info.state}"


class DiskFault(Fault):
    """Fill Paperless' 64 MB tmpfs scratch volume (where uploads land) to 100%.
    The webserver container is not touched; the host disk is never involved."""

    fault_type = config.FAULT_DISK

    def preflight(self) -> list[Check]:
        try:
            self.docker.verify_scratch_volume()
            use = self.docker.disk_usage(self.run_id)
            return [Check("bounded scratch volume present", True, config.SCRATCH_VOLUME),
                    Check("scratch volume not already full", use["used_pct"] < 50, f"{use['used_pct']}% used")]
        except DockerError as e:
            return [Check("bounded scratch volume present", False, str(e))]

    def inject(self) -> None:
        self.docker.disk_fill_start(self.run_id, self.spec["duration_s"] + config.LIMITS.watchdog_grace_s)
        use = {"used_pct": 0}
        for _ in range(10):   # the filler runs detached: confirm the disk really is full
            use = self.docker.disk_usage(self.run_id + "-c")
            if use["used_pct"] >= 95:
                return
            time.sleep(1.0)
        raise DockerError(f"scratch volume only {use['used_pct']}% full after filling")

    def remove(self) -> None:
        for sid in self.docker.disk_sidecars():
            self.docker.remove_netem_sidecar(sid)
        self.docker.disk_clear(self.run_id)

    def verify_removed(self) -> tuple[bool, str]:
        use = self.docker.disk_usage(self.run_id + "-v")
        return use["used_pct"] < 90, f"scratch {use['used_pct']}% used"


FAULTS: dict[str, type[Fault]] = {
    config.FAULT_REFUSE: FirewallFault,
    config.FAULT_MEMORY: MemoryFault,
    config.FAULT_DISK: DiskFault,
    config.FAULT_PAUSE: PauseFault,
    config.FAULT_LATENCY: LatencyFault,
    config.FAULT_LOSS: PacketLossFault,
    config.FAULT_STOP: StopFault,
    config.FAULT_CPU: CpuFault,
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
        if not c.sandbox or c.service not in config.SERVICE_PORTS:
            continue   # only dependencies get container-level faults; the webserver's disk is swept below
        try:
            if c.paused:
                docker.unpause(c.id)
                notes.append(f"unpaused {c.service}")
            if not c.running and not c.paused:
                docker.start(c.id)
                notes.append(f"started {c.service}")
                c = docker.inspect(c.id)
            if c.cpu_limit and not c.nano_cpus:
                docker.set_cpus(c.id, 0)
                notes.append(f"removed CPU quota on {c.service}")
            if c.memory:
                docker.recreate_service(c.service)
                notes.append(f"recreated {c.service} to lift its memory limit")
                c = docker.resolve_service(c.service)
            if netem_ready and c.running and "CHAOS" in docker.firewall_show(c.id, "sweep-fw"):
                docker.firewall_clear(c.id, "sweep-fw")
                notes.append(f"removed connection-reject rule on {c.service}")
            if netem_ready and c.running:
                if "netem" in docker.netem_show(c.id, "sweep"):
                    docker.netem_clear(c.id, "sweep")
                    notes.append(f"cleared netem on {c.service}")
                if "netem" in docker.netem_show(c.id, "sweep-v"):
                    ok = False
                    notes.append(f"netem still present on {c.service}")
            after = docker.inspect(c.id)
            if after.paused or not after.running or (after.cpu_limit and not after.nano_cpus) or after.memory:
                ok = False
                notes.append(f"{c.service} not restored (state={after.state}, cpu_limit={after.cpu_limit:g})")
        except Exception as e:
            ok = False
            notes.append(f"{c.service}: {e}")
    try:
        if netem_ready and docker.disk_usage("sweep-disk")["used_pct"] >= 90:
            docker.disk_clear("sweep-disk")
            notes.append("freed the scratch volume")
            if docker.disk_usage("sweep-disk-v")["used_pct"] >= 90:
                ok = False
                notes.append("scratch volume still full")
    except NotInSandbox:
        pass   # no scratch volume in this sandbox: nothing to sweep
    except Exception as e:
        ok = False
        notes.append(f"scratch volume: {e}")
    return ok, notes


def _safe(fn, default):
    try:
        return fn()
    except Exception:
        return default
