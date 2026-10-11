"""Thin Paperless-ngx REST client that turns real application calls into
timed ProbeResults."""
from __future__ import annotations

import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

import httpx

from . import config
from .models import ProbeResult

API_HEADERS = {"Accept": "application/json; version=10"}


def read_sandbox_env() -> dict[str, str]:
    env: dict[str, str] = {}
    if config.SANDBOX_ENV_FILE.exists():
        for line in config.SANDBOX_ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


class PaperlessClient:
    def __init__(self, base_url: str = config.PAPERLESS_URL, username: str | None = None,
                 password: str | None = None, timeout: float = 5.0):
        env = read_sandbox_env()
        self.base_url = base_url.rstrip("/")
        self.username = username or env.get("PAPERLESS_ADMIN_USER", "chaosadmin")
        self.password = password or env.get("PAPERLESS_ADMIN_PASSWORD", "")
        self.timeout = timeout
        self._token: Optional[str] = None
        self._token_lock = threading.Lock()

    def _client(self) -> httpx.Client:
        return httpx.Client(base_url=self.base_url, timeout=self.timeout, follow_redirects=True)

    def token(self) -> str:
        with self._token_lock:
            if self._token:
                return self._token
            with self._client() as c:
                r = c.post("/api/token/", data={"username": self.username, "password": self.password})
                r.raise_for_status()
                self._token = r.json()["token"]
            return self._token

    def _auth(self) -> dict[str, str]:
        return {**API_HEADERS, "Authorization": f"Token {self.token()}"}

    def _timed(self, probe: str, fn) -> ProbeResult:
        start = time.time()
        t0 = time.perf_counter()
        try:
            ok, status, detail = fn()
        except httpx.TimeoutException as e:
            ok, status, detail = False, None, f"timeout after {self.timeout}s ({type(e).__name__})"
        except httpx.HTTPError as e:
            ok, status, detail = False, None, f"{type(e).__name__}: {e}"[:300]
        except Exception as e:  # noqa: BLE001 - every failure is a data point
            ok, status, detail = False, None, f"{type(e).__name__}: {e}"[:300]
        return ProbeResult(probe, ok, (time.perf_counter() - t0) * 1000, status, detail, start)

    # -- probes -------------------------------------------------------------------
    def probe_web_root(self) -> ProbeResult:
        def fn():
            with self._client() as c:
                r = c.get("/")
            return r.status_code < 400, r.status_code, f"{r.status_code} {r.url.path}"
        return self._timed("web_root", fn)

    def probe_documents(self) -> ProbeResult:
        def fn():
            with self._client() as c:
                r = c.get("/api/documents/", params={"page_size": 10}, headers=self._auth())
            ok = r.status_code == 200
            detail = f"count={r.json().get('count')}" if ok else r.text[:200]
            return ok, r.status_code, detail
        return self._timed("api_documents", fn)

    def system_status(self) -> dict:
        with self._client() as c:
            r = c.get("/api/status/", headers=self._auth())
            r.raise_for_status()
            return r.json()

    def probe_status(self) -> ProbeResult:
        """Paperless' own view of its dependencies (database + redis/celery)."""
        def fn():
            with self._client() as c:
                r = c.get("/api/status/", headers=self._auth())
            if r.status_code != 200:
                return False, r.status_code, r.text[:200]
            d = r.json()
            db = d.get("database", {}).get("status")
            redis = d.get("tasks", {}).get("redis_status")
            celery = d.get("tasks", {}).get("celery_status")
            ok = db == "OK" and redis == "OK"
            return ok, r.status_code, f"db={db} redis={redis} celery={celery}"
        return self._timed("api_status", fn)

    def upload_document(self) -> tuple[ProbeResult, Optional[str]]:
        task_id: list[str] = []

        def fn():
            uid = uuid.uuid4().hex[:12]
            now = datetime.now(timezone.utc).isoformat()
            body = (f"Chaos sandbox synthetic document {uid}\nCreated: {now}\n"
                    f"Invoice reference CHAOS-{uid.upper()}\nAmount: 42.00 EUR\n").encode()
            with self._client() as c:
                r = c.post("/api/documents/post_document/", headers=self._auth(),
                           files={"document": (f"chaos-{uid}.txt", body, "text/plain")},
                           data={"title": f"chaos synthetic {uid}"})
            if r.status_code != 200:
                return False, r.status_code, r.text[:200]
            tid = r.json()
            task_id.append(str(tid))
            return True, r.status_code, f"task={tid}"

        res = self._timed("doc_upload", fn)
        return res, (task_id[0] if task_id else None)

    def task_queue(self) -> int:
        """Document tasks waiting for or being worked on by the worker."""
        with self._client() as c:
            r = c.get("/api/tasks/", params={"acknowledged": "false"}, headers=self._auth())
        r.raise_for_status()
        data = r.json()
        items = data.get("results", []) if isinstance(data, dict) else data
        return sum(1 for t in items if str(t.get("status", "")).upper() in ("PENDING", "STARTED", "RECEIVED"))

    def task_status(self, task_id: str) -> Optional[dict]:
        with self._client() as c:
            r = c.get("/api/tasks/", params={"task_id": task_id}, headers=self._auth())
        r.raise_for_status()
        data = r.json()
        items = data.get("results", []) if isinstance(data, dict) else data
        return items[0] if items else None
