"""Always-on light monitor used by the Overview page.

Records real probes + container health under run_id 'monitor' every few
seconds and keeps the latest snapshot in memory. On a slower cadence it also
collects the website's basic metrics: documents stored, task queue length,
CPU / memory per container (docker stats) and the upload scratch disk.
"""
from __future__ import annotations

import re
import threading
import time
from collections import deque

from .docker_ops import Docker, DockerError
from .storage import Store

MONITOR_RUN = "monitor"
RETENTION_S = 6 * 3600
SLOW_EVERY = 3        # documents, queue and docker stats every 3rd tick (~15 s)
DISK_EVERY = 6        # scratch disk every 6th tick (~30 s): it costs a helper container
HISTORY = 200         # ~15 minutes of slow-cadence points


class Monitor:
    def __init__(self, store: Store, docker: Docker, client_factory, interval_s: float = 5.0):
        self.store = store
        self.docker = docker
        self.client_factory = client_factory
        self.interval_s = interval_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.latest: dict = {}
        self.history: deque = deque(maxlen=HISTORY)   # {ts, documents, queue}
        self.resources: dict = {}
        self.disk: dict | None = None
        self._last_prune = 0.0
        self._ticks = 0

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="monitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        client = self.client_factory()
        while not self._stop.is_set():
            started = time.time()
            try:
                self.tick(client)
            except Exception as e:  # noqa: BLE001
                self.latest = {**self.latest, "error": f"{type(e).__name__}: {e}", "ts": time.time()}
            self._stop.wait(max(0.5, self.interval_s - (time.time() - started)))

    def tick(self, client) -> dict:
        now = time.time()
        self._ticks += 1
        services, docker_error, containers = [], None, []
        try:
            containers = self.docker.project_containers()
            for c in containers:
                state = "paused" if c.paused else c.state
                services.append({"service": c.service, "state": state, "health": c.health,
                                 "ok": c.ok, "id": c.id[:12], "started_at": c.started_at,
                                 "cpu_limit": c.cpu_limit, "memory_limit": c.memory})
                self.store.add_health(MONITOR_RUN, "idle", c.service, state, c.health, now)
        except DockerError as e:
            docker_error = str(e)
        probes = []
        for r in (client.probe_web_root(), client.probe_status()):
            self.store.add_sample(MONITOR_RUN, "idle", r)
            probes.append(r.to_dict())
        if self._ticks % SLOW_EVERY == 1:
            self._slow(client, containers, now)
        if self._ticks % DISK_EVERY == 1:
            try:
                self.disk = self.docker.disk_usage("monitor")
            except Exception:  # noqa: BLE001 - no scratch volume, or Docker busy
                self.disk = None
        self.latest = {"ts": now, "services": services, "probes": probes, "docker_error": docker_error,
                       "app_ok": all(p["ok"] for p in probes)}
        if now - self._last_prune > 300:
            self.store.prune(MONITOR_RUN, now - RETENTION_S)
            self._last_prune = now
        return self.latest

    def _slow(self, client, containers, now: float) -> None:
        point = {"ts": now, "documents": None, "queue": None}
        r = client.probe_documents()
        self.store.add_sample(MONITOR_RUN, "idle", r)
        m = re.search(r"count=(\d+)", r.detail or "")
        if m:
            point["documents"] = int(m.group(1))
        try:
            point["queue"] = client.task_queue()
        except Exception:  # noqa: BLE001 - Paperless busy or down: no point for this tick
            pass
        self.history.append(point)
        running = [c for c in containers if c.running and not c.paused]
        try:
            usage = self.docker.stats([c.id for c in running])
            self.resources = {c.service: {**u, "cpu_limit": c.cpu_limit, "memory_limit": c.memory}
                              for c in running for cid, u in usage.items()
                              if cid and (c.id.startswith(cid) or cid.startswith(c.id))}
        except Exception:  # noqa: BLE001
            pass
