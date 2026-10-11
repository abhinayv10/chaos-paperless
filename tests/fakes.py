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
        self.state = {s: {"paused": False, "running": True, "health": "healthy", "sandbox": True,
                          "netem": False, "cpus": 0.0, "fw": False, "mem": 0, "restarts": 0} for s in IDS}
        self.scratch_pct = 0.0
        self.helper_image = False   # the sidecar image; most tests run without it
        self.has_scratch = True
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
            cpu_quota=int(st["cpus"] * 100000), cpu_period=100000 if st["cpus"] else 0,
            memory=st["mem"] * 2 ** 20, restart_count=st["restarts"], oom_killed=bool(st["mem"]),
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

    def stop(self, cid):
        self.calls.append(("stop", cid))
        self.verify_sandbox(cid)
        with self.lock:
            self.state[self._svc(cid)]["running"] = False

    def start(self, cid):
        self.calls.append(("start", cid))
        self.verify_sandbox(cid)
        with self.lock:
            self.state[self._svc(cid)]["running"] = True

    def set_cpus(self, cid, cpus):
        self.calls.append(("set_cpus", cid, cpus))
        self.verify_sandbox(cid)
        with self.lock:
            self.state[self._svc(cid)]["cpus"] = float(cpus)

    def set_memory(self, cid, mb):
        self.calls.append(("set_memory", cid, mb))
        self.verify_sandbox(cid)
        with self.lock:
            self.state[self._svc(cid)]["mem"] = int(mb)

    def recreate_service(self, service):
        self.calls.append(("recreate", service))
        with self.lock:
            self.state[service].update(mem=0, cpus=0.0, paused=False, running=True, fw=False, restarts=0)

    def firewall_start(self, cid, run_id, port, duration_s):
        self.calls.append(("firewall_start", cid, port))
        self.verify_sandbox(cid)
        with self.lock:
            self.state[self._svc(cid)]["fw"] = True

    def firewall_show(self, cid, run_id="verify"):
        return "-N CHAOS\n-A INPUT -j CHAOS" if self.state[self._svc(cid)]["fw"] else "-P INPUT ACCEPT"

    def firewall_clear(self, cid, run_id="cleanup"):
        self.calls.append(("firewall_clear", cid))
        with self.lock:
            self.state[self._svc(cid)]["fw"] = False

    def verify_scratch_volume(self):
        from chaos.docker_ops import NotInSandbox
        if not self.has_scratch:
            raise NotInSandbox("no scratch volume")
        return {}

    def disk_fill_start(self, run_id, duration_s):
        self.verify_scratch_volume()
        self.calls.append(("disk_fill", run_id))
        self.scratch_pct = 100.0

    def disk_clear(self, run_id="cleanup"):
        self.verify_scratch_volume()
        self.calls.append(("disk_clear", run_id))
        self.scratch_pct = 0.0

    def disk_usage(self, run_id="usage"):
        self.verify_scratch_volume()
        return {"total_mb": 64.0, "used_mb": 0.64 * self.scratch_pct, "used_pct": self.scratch_pct}

    def disk_sidecars(self):
        return []

    def stats(self, ids):
        out = {}
        for cid in ids:
            st = self.state[self._svc(cid)]
            out[cid] = {"cpu_pct": st["cpus"] * 100 * 0.98 if st["cpus"] else 1.5,
                        "mem_pct": 99.0 if st["mem"] else 3.0}
        return out

    def netem_sidecars(self):
        return []

    def netem_sidecars_for(self, cid):
        return []

    def netem_show(self, cid, run_id="verify"):
        self.verify_sandbox(cid)
        return "qdisc netem 8001: root refcnt 2 delay 300ms" if self.state[self._svc(cid)]["netem"] \
            else "qdisc noqueue 0: dev eth0 root refcnt 2"

    def netem_clear(self, cid, run_id="cleanup"):
        self.calls.append(("netem_clear", cid))
        self.verify_sandbox(cid)
        with self.lock:
            self.state[self._svc(cid)]["netem"] = False

    def image_present(self, image):
        return self.helper_image

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
        st = self.docker.state
        refused = [d for d in deps if st[d]["fw"]]
        if refused:   # rejected connections fail at once, unlike a hang
            return ProbeResult(probe, False, 60.0, 500, f"connection refused ({','.join(refused)})", time.time())
        bad = [d for d in deps if st[d]["paused"] or st[d]["netem"] or not st[d]["running"] or st[d]["cpus"]
               or st[d]["mem"]]
        if bad:
            return ProbeResult(probe, False, self.slow_ms, None, f"timeout ({','.join(bad)} not answering)",
                               time.time())
        return ProbeResult(probe, True, 20.0, 200, "ok", time.time())

    def probe_web_root(self):
        return self._r("web_root", ["db"])

    def probe_documents(self):
        return self._r("api_documents", ["db"])

    def probe_status(self):
        return self._r("api_status", ["db", "broker"])

    def upload_document(self):
        r = self._r("doc_upload", ["db", "broker"])  # real uploads need db too (token auth)
        if r.ok and self.docker.scratch_pct >= 95:
            r = ProbeResult("doc_upload", False, 80.0, 500, "Server Error (500)", time.time())
        return r, (f"task-{next(self._ids)}" if r.ok else None)

    def task_queue(self):
        return 0

    def task_status(self, task_id):
        return {"status": "success", "duration_seconds": 0.1, "wait_time_seconds": 0.0}
