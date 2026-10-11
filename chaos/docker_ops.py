"""The only module that runs the Docker CLI.

It exposes a small set of predefined operations. Arguments are always passed
as an argv list (never through a shell), and every container-level operation
first re-resolves the container and checks it belongs to the sandbox compose
project *and* carries the chaos.sandbox=true label.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from typing import Optional

from . import config

_ID_RE = re.compile(r"^[0-9a-f]{12,64}$")


class DockerError(RuntimeError):
    pass


class NotInSandbox(DockerError):
    pass


@dataclass
class ContainerInfo:
    id: str
    name: str
    service: str
    project: str
    image: str
    state: str  # running / paused / exited / ...
    health: str  # healthy / unhealthy / starting / none
    paused: bool
    running: bool
    sandbox: bool
    labels: dict = field(default_factory=dict)
    env: list = field(default_factory=list)
    networks: dict = field(default_factory=dict)
    ports: dict = field(default_factory=dict)
    started_at: str = ""
    nano_cpus: int = 0   # HostConfig.NanoCpus (docker --cpus)
    cpu_quota: int = 0   # HostConfig.CpuQuota in us per period; 0 or -1 = unlimited
    cpu_period: int = 0  # HostConfig.CpuPeriod in us
    memory: int = 0      # HostConfig.Memory in bytes; 0 = no limit
    oom_killed: bool = False
    restart_count: int = 0

    @property
    def cpu_limit(self) -> float:
        """Effective CPU cap in CPUs; 0.0 means unlimited."""
        if self.nano_cpus:
            return self.nano_cpus / 1e9
        if self.cpu_quota > 0:
            return self.cpu_quota / (self.cpu_period or 100000)
        return 0.0

    @property
    def ok(self) -> bool:
        return self.running and not self.paused and self.health in ("healthy", "none")

    def to_dict(self) -> dict:
        return {
            "id": self.id[:12], "name": self.name, "service": self.service,
            "image": self.image, "state": self.state, "health": self.health,
            "paused": self.paused, "running": self.running, "sandbox": self.sandbox,
            "networks": self.networks, "ports": self.ports, "started_at": self.started_at,
        }


def _from_inspect(d: dict) -> ContainerInfo:
    labels = d.get("Config", {}).get("Labels") or {}
    st = d.get("State", {})
    health = (st.get("Health") or {}).get("Status", "none")
    nets = {
        name: (v or {}).get("IPAddress", "")
        for name, v in ((d.get("NetworkSettings") or {}).get("Networks") or {}).items()
    }
    ports = {}
    for cport, binds in ((d.get("NetworkSettings") or {}).get("Ports") or {}).items():
        if binds:
            ports[cport] = [f"{b.get('HostIp', '')}:{b.get('HostPort', '')}" for b in binds]
    return ContainerInfo(
        id=d["Id"],
        name=d.get("Name", "").lstrip("/"),
        service=labels.get("com.docker.compose.service", ""),
        project=labels.get("com.docker.compose.project", ""),
        image=d.get("Config", {}).get("Image", ""),
        state=st.get("Status", "unknown"),
        health=health,
        paused=bool(st.get("Paused")),
        running=bool(st.get("Running")),
        sandbox=labels.get(config.SANDBOX_LABEL) == "true",
        labels=labels,
        env=d.get("Config", {}).get("Env") or [],
        networks=nets,
        ports=ports,
        started_at=st.get("StartedAt", ""),
        nano_cpus=int((d.get("HostConfig") or {}).get("NanoCpus") or 0),
        cpu_quota=int((d.get("HostConfig") or {}).get("CpuQuota") or 0),
        cpu_period=int((d.get("HostConfig") or {}).get("CpuPeriod") or 0),
        memory=int((d.get("HostConfig") or {}).get("Memory") or 0),
        oom_killed=bool(st.get("OOMKilled")),
        restart_count=int(d.get("RestartCount") or 0),
    )


class Docker:
    def __init__(self, binary: str | None = None, project: str = config.COMPOSE_PROJECT):
        self.binary = binary or config.docker_bin()
        self.project = project

    # -- low level ---------------------------------------------------------------
    def _run(self, args: list[str], timeout: float = 30, check: bool = True) -> subprocess.CompletedProcess:
        # docker shells out to helpers (docker-credential-desktop, compose
        # plugins) that live next to the binary; make sure they resolve even
        # when Docker's bin dir is not on this process' PATH.
        env = dict(os.environ)
        bindir = os.path.dirname(self.binary)
        if bindir:
            env["PATH"] = bindir + os.pathsep + env.get("PATH", "")
        try:
            cp = subprocess.run(
                [self.binary, *args], capture_output=True, text=True, timeout=timeout,
                encoding="utf-8", errors="replace", env=env,
            )
        except FileNotFoundError as e:
            raise DockerError(f"docker CLI not found ({self.binary})") from e
        except subprocess.TimeoutExpired as e:
            raise DockerError(f"docker {args[0]} timed out after {timeout}s") from e
        if check and cp.returncode != 0:
            raise DockerError(f"docker {' '.join(args[:2])} failed: {cp.stderr.strip()[:400]}")
        return cp

    # -- read-only ---------------------------------------------------------------
    def available(self) -> tuple[bool, str]:
        try:
            cp = self._run(["version", "--format", "{{.Server.Version}}"], timeout=15)
            return True, cp.stdout.strip()
        except DockerError as e:
            return False, str(e)

    def project_containers(self) -> list[ContainerInfo]:
        cp = self._run(["ps", "-aq", "--no-trunc", "--filter",
                        f"label=com.docker.compose.project={self.project}"])
        ids = [i for i in cp.stdout.split() if _ID_RE.match(i)]
        if not ids:
            return []
        return [_from_inspect(d) for d in self._inspect(ids)]

    def _inspect(self, ids: list[str]) -> list[dict]:
        cp = self._run(["inspect", *ids])
        return json.loads(cp.stdout)

    def inspect(self, container_id: str) -> ContainerInfo:
        _require_id(container_id)
        return _from_inspect(self._inspect([container_id])[0])

    def resolve_service(self, service: str) -> ContainerInfo:
        """Map a compose service name to its sandbox container, or raise."""
        matches = [c for c in self.project_containers() if c.service == service]
        if not matches:
            raise NotInSandbox(f"no container for service '{service}' in project {self.project}")
        if len(matches) > 1:
            raise NotInSandbox(f"service '{service}' has {len(matches)} containers; refusing ambiguity")
        return self.verify_sandbox(matches[0].id)

    def verify_sandbox(self, container_id: str) -> ContainerInfo:
        info = self.inspect(container_id)
        if info.project != self.project or not info.sandbox:
            raise NotInSandbox(f"container {container_id[:12]} is not an approved sandbox resource")
        return info

    def logs(self, container_id: str, since: float, until: float | None = None,
             tail: int = 200) -> list[str]:
        self.verify_sandbox(container_id)
        args = ["logs", "--timestamps", "--since", f"{since:.0f}", "--tail", str(int(tail))]
        if until:
            args += ["--until", f"{until:.0f}"]
        cp = self._run([*args, container_id], timeout=20, check=False)
        return (cp.stdout + cp.stderr).splitlines()

    # -- mutating (sandbox-only) -------------------------------------------------
    def pause(self, container_id: str) -> None:
        self.verify_sandbox(container_id)
        self._run(["pause", container_id])

    def unpause(self, container_id: str) -> None:
        info = self.verify_sandbox(container_id)
        if not info.paused:
            return  # idempotent
        self._run(["unpause", container_id])

    def stop(self, container_id: str) -> None:
        self.verify_sandbox(container_id)
        self._run(["stop", "-t", "10", container_id], timeout=40)

    def start(self, container_id: str) -> None:
        info = self.verify_sandbox(container_id)
        if info.running:
            return  # idempotent
        self._run(["start", container_id], timeout=40)

    def set_cpus(self, container_id: str, cpus: float) -> None:
        """CPU cap in CPUs via the CFS quota; 0 removes it (--cpu-quota -1).
        (docker --cpus / NanoCpus cannot be reset to unlimited by an update,
        so it is deliberately not used.)"""
        self.verify_sandbox(container_id)
        cpus = float(cpus)
        if not 0 <= cpus <= 64:
            raise DockerError("invalid cpu quota")
        if cpus == 0:
            self._run(["update", "--cpu-quota", "-1", container_id])
        else:
            self._run(["update", "--cpu-period", "100000", "--cpu-quota", str(max(1000, int(cpus * 100000))),
                       container_id])

    def set_memory(self, container_id: str, mb: int) -> None:
        """Tight memory limit (no swap). Docker cannot remove a memory limit with
        an update, so removal is recreate_service()."""
        self.verify_sandbox(container_id)
        mb = int(mb)
        if not 6 <= mb <= 4096:
            raise DockerError("invalid memory limit")
        self._run(["update", "--memory", f"{mb}m", "--memory-swap", f"{mb}m", container_id])

    def recreate_service(self, service: str) -> None:
        """Recreate one allowlisted service from its compose definition (resets
        every runtime change: limits, state). Data lives on named volumes."""
        if service not in config.LIMITS.allowlist:
            raise NotInSandbox(f"service '{service}' is not an approved sandbox resource")
        cp = self.compose("up", "-d", "--force-recreate", "--no-deps", service, timeout=240)
        if cp.returncode != 0:
            raise DockerError(f"compose recreate {service} failed: {cp.stderr.strip()[-300:]}")

    def stats(self, container_ids: list[str]) -> dict[str, dict]:
        """One-shot CPU / memory usage per container (docker stats --no-stream)."""
        for cid in container_ids:
            _require_id(cid)
        if not container_ids:
            return {}
        cp = self._run(["stats", "--no-stream", "--no-trunc", "--format", "{{json .}}", *container_ids],
                       timeout=30)
        out = {}
        for line in cp.stdout.splitlines():
            try:
                d = json.loads(line)
            except ValueError:
                continue
            pct = lambda v: float(str(v).rstrip("%") or 0)  # noqa: E731
            used, _, limit = str(d.get("MemUsage", "")).partition("/")
            out[d.get("ID", "")] = {"cpu_pct": pct(d.get("CPUPerc", "0")), "mem_pct": pct(d.get("MemPerc", "0")),
                                    "mem_bytes": _bytes(used), "mem_total_bytes": _bytes(limit)}
        return out

    # -- netem sidecar (latency fault) ---------------------------------------------
    def image_present(self, image: str) -> bool:
        return self._run(["image", "inspect", image], check=False).returncode == 0

    def _netem_base(self, container_id: str, run_id: str) -> list[str]:
        self.verify_sandbox(container_id)
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", run_id):
            raise DockerError("invalid run id")
        return [
            "run", "--rm", "--network", f"container:{container_id}",
            "--cap-drop", "ALL", "--cap-add", "NET_ADMIN",
            "--security-opt", "no-new-privileges", "--memory", "32m", "--pids-limit", "16",
            "--label", f"{config.NETEM_LABEL}=true", "--label", f"chaos.run={run_id}",
        ]

    def netem_start(self, container_id: str, run_id: str, latency_ms: int | None, duration_s: int,
                    loss_pct: int | None = None) -> str:
        """Start a detached sidecar that adds the delay (or packet loss) and
        removes it itself after duration_s (so the fault is self-expiring)."""
        duration_s = int(duration_s)
        if loss_pct is not None:
            impair = f"loss {int(loss_pct)}%"
        elif latency_ms is not None:
            impair = f"delay {int(latency_ms)}ms"
        else:
            raise DockerError("netem needs latency_ms or loss_pct")
        script = (
            f"tc qdisc replace dev eth0 root netem {impair} && "
            f"trap 'tc qdisc del dev eth0 root 2>/dev/null; exit 0' TERM INT; "
            f"sleep {duration_s} & wait; tc qdisc del dev eth0 root 2>/dev/null; exit 0"
        )
        args = self._netem_base(container_id, run_id)
        args[1:1] = ["-d", "--name", f"chaos-netem-{run_id}"]
        cp = self._run([*args, config.NETEM_IMAGE, "sh", "-c", script], timeout=30)
        return cp.stdout.strip()

    def netem_show(self, container_id: str, run_id: str = "verify") -> str:
        args = self._netem_base(container_id, run_id)
        return self._run([*args, config.NETEM_IMAGE, "tc", "qdisc", "show", "dev", "eth0"],
                         timeout=30).stdout

    def netem_clear(self, container_id: str, run_id: str = "cleanup") -> None:
        args = self._netem_base(container_id, run_id)
        # 'del' fails harmlessly when no root qdisc is configured
        self._run([*args, config.NETEM_IMAGE, "tc", "qdisc", "del", "dev", "eth0", "root"],
                  timeout=30, check=False)

    # -- firewall sidecar (connection_refused) ---------------------------------------------
    def firewall_start(self, container_id: str, run_id: str, port: int, duration_s: int) -> str:
        """Reject every TCP packet to `port` in the target's network namespace
        (clients get an immediate connection reset). Self-expiring like netem."""
        port, duration_s = int(port), int(duration_s)
        clear = "iptables -D INPUT -j CHAOS 2>/dev/null; iptables -F CHAOS 2>/dev/null; iptables -X CHAOS 2>/dev/null"
        script = (
            f"iptables -N CHAOS && iptables -I INPUT -j CHAOS && "
            f"iptables -A CHAOS -p tcp --dport {port} -j REJECT --reject-with tcp-reset && "
            f"trap '{clear}; exit 0' TERM INT; sleep {duration_s} & wait; {clear}; exit 0"
        )
        args = self._netem_base(container_id, run_id)
        args[1:1] = ["-d", "--name", f"chaos-fw-{run_id}"]
        return self._run([*args, config.NETEM_IMAGE, "sh", "-c", script], timeout=30).stdout.strip()

    def firewall_show(self, container_id: str, run_id: str = "verify") -> str:
        args = self._netem_base(container_id, run_id)
        return self._run([*args, config.NETEM_IMAGE, "iptables", "-S"], timeout=30).stdout

    def firewall_clear(self, container_id: str, run_id: str = "cleanup") -> None:
        args = self._netem_base(container_id, run_id)
        for cmd in (["iptables", "-D", "INPUT", "-j", "CHAOS"], ["iptables", "-F", "CHAOS"],
                    ["iptables", "-X", "CHAOS"]):
            self._run([*args, config.NETEM_IMAGE, *cmd], timeout=30, check=False)

    # -- scratch volume (disk_exhaustion) ---------------------------------------------------
    def verify_scratch_volume(self) -> dict:
        cp = self._run(["volume", "inspect", config.SCRATCH_VOLUME], check=False)
        if cp.returncode != 0:
            raise NotInSandbox(f"scratch volume {config.SCRATCH_VOLUME} not found (run: python -m chaos up)")
        v = json.loads(cp.stdout)[0]
        labels = v.get("Labels") or {}
        if labels.get("com.docker.compose.project") != self.project or labels.get(config.SANDBOX_LABEL) != "true":
            raise NotInSandbox("scratch volume is not an approved sandbox resource")
        if (v.get("Options") or {}).get("type") != "tmpfs":
            raise NotInSandbox("scratch volume is not a bounded tmpfs; refusing to fill it")
        return v

    def _scratch_base(self, run_id: str, memory: str = "32m") -> list[str]:
        self.verify_scratch_volume()
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", run_id):
            raise DockerError("invalid run id")
        return ["run", "--rm", "--network", "none", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                "--memory", memory, "--pids-limit", "16", "-v", f"{config.SCRATCH_VOLUME}:/scratch",
                "--label", f"{config.NETEM_LABEL}=true", "--label", f"chaos.run={run_id}"]

    def disk_fill_start(self, run_id: str, duration_s: int) -> str:
        """Fill the scratch volume to 100% and delete the filler after duration_s."""
        f = f"/scratch/{config.SCRATCH_FILL}"
        script = (f"dd if=/dev/zero of={f} bs=1M 2>/dev/null; trap 'rm -f {f}; exit 0' TERM INT; "
                  f"sleep {int(duration_s)} & wait; rm -f {f}; exit 0")
        # written tmpfs pages count against the writer's memory limit: leave room for the whole volume
        args = self._scratch_base(run_id, memory="160m")
        args[1:1] = ["-d", "--name", f"chaos-disk-{run_id}"]
        return self._run([*args, config.NETEM_IMAGE, "sh", "-c", script], timeout=30).stdout.strip()

    def disk_clear(self, run_id: str = "cleanup") -> None:
        self._run([*self._scratch_base(run_id), config.NETEM_IMAGE, "rm", "-f", f"/scratch/{config.SCRATCH_FILL}"],
                  timeout=30, check=False)

    def disk_usage(self, run_id: str = "usage") -> dict:
        out = self._run([*self._scratch_base(run_id), config.NETEM_IMAGE, "df", "-Pk", "/scratch"],
                        timeout=30).stdout.strip().splitlines()[-1].split()
        total, used = int(out[1]), int(out[2])
        return {"total_mb": round(total / 1024, 1), "used_mb": round(used / 1024, 1),
                "used_pct": round(100 * used / total, 1) if total else 0.0}

    def disk_sidecars(self) -> list[str]:
        cp = self._run(["ps", "-aq", "--no-trunc", "--filter", f"label={config.NETEM_LABEL}=true",
                        "--filter", "name=chaos-disk-"])
        return [i for i in cp.stdout.split() if _ID_RE.match(i)]

    def netem_sidecars(self) -> list[str]:
        cp = self._run(["ps", "-aq", "--no-trunc", "--filter", f"label={config.NETEM_LABEL}=true"])
        return [i for i in cp.stdout.split() if _ID_RE.match(i)]

    def netem_sidecars_for(self, container_id: str) -> list[str]:
        """Netem sidecars attached to one target's network namespace."""
        _require_id(container_id)
        ids = self.netem_sidecars()
        if not ids:
            return []
        out = []
        for d in self._inspect(ids):
            mode = (d.get("HostConfig") or {}).get("NetworkMode") or ""
            ref = mode.split(":", 1)[1] if mode.startswith("container:") else ""
            if ref and (container_id.startswith(ref) or ref.startswith(container_id)):
                out.append(d["Id"])
        return out

    def remove_netem_sidecar(self, sidecar_id: str) -> None:
        _require_id(sidecar_id)
        d = self._inspect([sidecar_id])[0]
        if (d.get("Config", {}).get("Labels") or {}).get(config.NETEM_LABEL) != "true":
            raise NotInSandbox("refusing to remove a container that is not a chaos netem sidecar")
        self._run(["rm", "-f", sidecar_id], check=False)

    # -- lifecycle (CLI only, fixed arguments) ----------------------------------------
    def compose(self, *args: str, timeout: float = 900) -> subprocess.CompletedProcess:
        allowed = {"up", "down", "pull", "ps", "build", "logs"}
        if not args or args[0] not in allowed:
            raise DockerError(f"compose subcommand not allowed: {args[:1]}")
        return self._run(["compose", "-p", self.project, "-f", str(config.COMPOSE_FILE), *args],
                         timeout=timeout, check=False)

    def build_netem_image(self) -> subprocess.CompletedProcess:
        return self._run(["build", "-t", config.NETEM_IMAGE, str(config.SANDBOX_DIR / "netem")],
                         timeout=600, check=False)


_UNITS = {"b": 1, "kib": 2 ** 10, "mib": 2 ** 20, "gib": 2 ** 30, "tib": 2 ** 40,
          "kb": 1e3, "mb": 1e6, "gb": 1e9, "tb": 1e12}


def _bytes(text: str) -> int:
    """'33.54MiB' -> bytes (docker stats MemUsage parts)."""
    m = re.match(r"\s*([\d.]+)\s*([A-Za-z]+)", text or "")
    return int(float(m.group(1)) * _UNITS.get(m.group(2).lower(), 1)) if m else 0


def _require_id(container_id: str) -> None:
    if not _ID_RE.match(container_id or ""):
        raise DockerError("invalid container id")
