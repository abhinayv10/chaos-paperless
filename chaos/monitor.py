"""Always-on light monitor used by the Overview page between experiments.

Records real probes + container health under run_id 'monitor' every few
seconds and keeps the latest snapshot in memory.
"""
from __future__ import annotations

import threading
import time

from .docker_ops import Docker, DockerError
from .storage import Store

MONITOR_RUN = "monitor"
RETENTION_S = 6 * 3600


class Monitor:
    def __init__(self, store: Store, docker: Docker, client_factory, interval_s: float = 5.0):
        self.store = store
        self.docker = docker
        self.client_factory = client_factory
        self.interval_s = interval_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.latest: dict = {}
        self._last_prune = 0.0

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
        services, docker_error = [], None
        try:
            for c in self.docker.project_containers():
                state = "paused" if c.paused else c.state
                services.append({"service": c.service, "state": state, "health": c.health,
                                 "ok": c.ok, "id": c.id[:12]})
                self.store.add_health(MONITOR_RUN, "idle", c.service, state, c.health, now)
        except DockerError as e:
            docker_error = str(e)
        probes = []
        for r in (client.probe_web_root(), client.probe_status()):
            self.store.add_sample(MONITOR_RUN, "idle", r)
            probes.append(r.to_dict())
        self.latest = {"ts": now, "services": services, "probes": probes, "docker_error": docker_error,
                       "app_ok": all(p["ok"] for p in probes)}
        if now - self._last_prune > 300:
            self.store.prune(MONITOR_RUN, now - RETENTION_S)
            self._last_prune = now
        return self.latest
