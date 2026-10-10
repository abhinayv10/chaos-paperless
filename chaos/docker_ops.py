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

    def netem_start(self, container_id: str, run_id: str, latency_ms: int, duration_s: int) -> str:
        """Start a detached sidecar that adds the delay and removes it itself
        after duration_s (so the fault is self-expiring)."""
        latency_ms, duration_s = int(latency_ms), int(duration_s)
        script = (
            f"tc qdisc replace dev eth0 root netem delay {latency_ms}ms && "
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

    def netem_sidecars(self) -> list[str]:
        cp = self._run(["ps", "-aq", "--no-trunc", "--filter", f"label={config.NETEM_LABEL}=true"])
        return [i for i in cp.stdout.split() if _ID_RE.match(i)]

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


def _require_id(container_id: str) -> None:
    if not _ID_RE.match(container_id or ""):
        raise DockerError("invalid container id")
