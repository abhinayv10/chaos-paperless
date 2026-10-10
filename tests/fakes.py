"""In-memory stand-ins for Docker and Paperless used by the unit tests.

FakeClient derives probe outcomes from FakeDocker state, so pausing the
fake 'db' makes the fake app fail exactly like the real one does.
"""
from __future__ import annotations

import itertools
import threading
import time

from chaos.docker_ops import ContainerInfo, DockerError, NotInSandbox
from chaos.models import ProbeResult

IDS = {"webserver": "a" * 64, "db": "b" * 64, "broker": "c" * 64}


class FakeDocker:
    project = "paperless-chaos"

    def __init__(self):
        self.lock = threading.Lock()
        self.state = {s: {"paused": False, "running": True, "health": "healthy", "sandbox": True}
                      for s in IDS}
        self.calls: list[tuple] = []
        self.fail_unpause = False
        self.fail_pause = False
        self.reachable = True

    def _info(self, svc: str) -> ContainerInfo:
        st = self.state[svc]
        return ContainerInfo(
            id=IDS[svc], name=f"paperless-chaos-{svc}-1", service=svc, project=self.project,
            image=f"{svc}:test", state="paused" if st["paused"] else ("running" if st["running"] else "exited"),
            health=st["health"], paused=st["paused"], running=st["running"], sandbox=st["sandbox"],
            labels={"com.docker.compose.service": svc}, networks={"paperless-chaos_default": "172.18.0.2"},
            env=["PAPERLESS_DBHOST=db", "PAPERLESS_REDIS=redis://broker:6379"] if svc == "webserver" else [],
        )

    def _svc(self, cid: str) -> str:
        for s, i in IDS.items():
            if i == cid:
                return s
        raise DockerError("no such container")

    def available(self):
        return (True, "fake") if self.reachable else (False, "unreachable")

    def project_containers(self):
        with self.lock:
            return [self._info(s) for s in IDS]

    def inspect(self, cid):
        with self.lock:
            return self._info(self._svc(cid))

    def verify_sandbox(self, cid):
        info = self.inspect(cid)
        if not info.sandbox:
            raise NotInSandbox("not sandbox")
        return info

    def resolve_service(self, service):
        if service not in IDS:
            raise NotInSandbox(f"no container for {service}")
        return self.verify_sandbox(IDS[service])

    def pause(self, cid):
        self.calls.append(("pause", cid))
        self.verify_sandbox(cid)
        if self.fail_pause:
            raise DockerError("pause failed")
        with self.lock:
            self.state[self._svc(cid)]["paused"] = True

    def unpause(self, cid):
        self.calls.append(("unpause", cid))
        info = self.verify_sandbox(cid)
        if self.fail_unpause:
            raise DockerError("unpause failed")
        if not info.paused:
            return
        with self.lock:
            self.state[self._svc(cid)]["paused"] = False

    def netem_sidecars(self):
        return []

    def image_present(self, image):
        return False

    def logs(self, cid, since, until=None, tail=200):
        return ["2026-10-10T10:00:00Z OperationalError: connection timed out"]

    def paused(self, svc):
        return self.state[svc]["paused"]


class FakeClient:
    """Probe results follow the fake container state."""

    _ids = itertools.count(1)

    def __init__(self, docker: FakeDocker, slow_ms: float = 4000):
        self.docker = docker
        self.slow_ms = slow_ms

    def _r(self, probe, deps):
        bad = [d for d in deps if self.docker.state[d]["paused"]]
        if bad:
            return ProbeResult(probe, False, self.slow_ms, None, f"timeout ({','.join(bad)} paused)", time.time())
        return ProbeResult(probe, True, 20.0, 200, "ok", time.time())

    def probe_web_root(self):
        return self._r("web_root", ["db"])

    def probe_documents(self):
        return self._r("api_documents", ["db"])

    def probe_status(self):
        return self._r("api_status", ["db", "broker"])

    def upload_document(self):
        r = self._r("doc_upload", ["db", "broker"])  # real uploads need db too (token auth)
        return r, (f"task-{next(self._ids)}" if r.ok else None)

    def task_status(self, task_id):
        return {"status": "success", "duration_seconds": 0.1, "wait_time_seconds": 0.0}
