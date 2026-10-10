"""Local SQLite persistence for samples, runs, safety events and state.

A fresh connection is opened per operation so the API server, the experiment
thread and the out-of-process watchdog can all write safely (WAL mode).
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL, run_id TEXT NOT NULL, phase TEXT NOT NULL,
    probe TEXT NOT NULL, ok INTEGER NOT NULL, latency_ms REAL,
    status INTEGER, detail TEXT
);
CREATE INDEX IF NOT EXISTS samples_run ON samples(run_id, ts);
CREATE TABLE IF NOT EXISTS health (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL, run_id TEXT NOT NULL, phase TEXT NOT NULL,
    service TEXT NOT NULL, state TEXT, health TEXT
);
CREATE INDEX IF NOT EXISTS health_run ON health(run_id, ts);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL, state TEXT NOT NULL,
    config TEXT, created REAL, fault_start REAL, fault_end REAL, ended REAL,
    outcome TEXT, summary TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL, run_id TEXT, level TEXT NOT NULL,
    kind TEXT NOT NULL, message TEXT NOT NULL, data TEXT
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY, value TEXT NOT NULL, updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS plans (
    id TEXT PRIMARY KEY, created REAL NOT NULL, data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS analyses (
    run_id TEXT PRIMARY KEY, created REAL NOT NULL, data TEXT NOT NULL
);
"""

RUN_JSON_FIELDS = ("config", "outcome", "summary")


class Store:
    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else config.DB_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ---- samples / health -------------------------------------------------
    def add_sample(self, run_id: str, phase: str, r) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO samples (ts, run_id, phase, probe, ok, latency_ms, status, detail)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (r.ts or time.time(), run_id, phase, r.probe, int(r.ok), r.latency_ms,
                 r.status, (r.detail or "")[:500]),
            )

    def samples(self, run_id: str, since: float | None = None) -> list[dict]:
        q = "SELECT * FROM samples WHERE run_id=?"
        args: list[Any] = [run_id]
        if since is not None:
            q += " AND ts>=?"
            args.append(since)
        with self._conn() as c:
            return [dict(r) for r in c.execute(q + " ORDER BY ts", args)]

    def add_health(self, run_id: str, phase: str, service: str, state: str, health: str,
                   ts: float | None = None) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO health (ts, run_id, phase, service, state, health) VALUES (?,?,?,?,?,?)",
                (ts or time.time(), run_id, phase, service, state, health),
            )

    def health(self, run_id: str, since: float | None = None) -> list[dict]:
        q = "SELECT * FROM health WHERE run_id=?"
        args: list[Any] = [run_id]
        if since is not None:
            q += " AND ts>=?"
            args.append(since)
        with self._conn() as c:
            return [dict(r) for r in c.execute(q + " ORDER BY ts", args)]

    def prune(self, run_id: str, older_than: float) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM samples WHERE run_id=? AND ts<?", (run_id, older_than))
            c.execute("DELETE FROM health WHERE run_id=? AND ts<?", (run_id, older_than))

    # ---- runs ---------------------------------------------------------------
    def create_run(self, run_id: str, kind: str, state: str, cfg: dict | None) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO runs (id, kind, state, config, created) VALUES (?,?,?,?,?)",
                (run_id, kind, state, json.dumps(cfg) if cfg is not None else None, time.time()),
            )

    def update_run(self, run_id: str, **fields: Any) -> None:
        if not fields:
            return
        cols, vals = [], []
        for k, v in fields.items():
            if k not in {"state", "fault_start", "fault_end", "ended", "outcome", "summary"}:
                raise ValueError(f"unknown run field {k}")
            cols.append(f"{k}=?")
            vals.append(json.dumps(v) if k in RUN_JSON_FIELDS else v)
        with self._conn() as c:
            c.execute(f"UPDATE runs SET {', '.join(cols)} WHERE id=?", (*vals, run_id))

    def get_run(self, run_id: str) -> Optional[dict]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return _decode_run(row) if row else None

    def list_runs(self, kind: str | None = None, limit: int = 50) -> list[dict]:
        q, args = "SELECT * FROM runs", []
        if kind:
            q += " WHERE kind=?"
            args.append(kind)
        with self._conn() as c:
            rows = c.execute(q + " ORDER BY created DESC LIMIT ?", (*args, limit)).fetchall()
        return [_decode_run(r) for r in rows]

    def latest_run(self, kind: str, state: str | None = None) -> Optional[dict]:
        q, args = "SELECT * FROM runs WHERE kind=?", [kind]
        if state:
            q += " AND state=?"
            args.append(state)
        with self._conn() as c:
            row = c.execute(q + " ORDER BY created DESC LIMIT 1", args).fetchone()
        return _decode_run(row) if row else None

    # ---- events ---------------------------------------------------------------
    def event(self, kind: str, message: str, run_id: str | None = None, level: str = "info",
              data: Any = None) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO events (ts, run_id, level, kind, message, data) VALUES (?,?,?,?,?,?)",
                (time.time(), run_id, level, kind, message,
                 json.dumps(data) if data is not None else None),
            )

    def events(self, run_id: str | None = None, limit: int = 200,
               kinds: tuple[str, ...] | None = None) -> list[dict]:
        q, args = "SELECT * FROM events WHERE 1=1", []
        if run_id:
            q += " AND run_id=?"
            args.append(run_id)
        if kinds:
            q += f" AND kind IN ({','.join('?' * len(kinds))})"
            args.extend(kinds)
        with self._conn() as c:
            rows = c.execute(q + " ORDER BY id DESC LIMIT ?", (*args, limit)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["data"] = json.loads(d["data"]) if d["data"] else None
            out.append(d)
        return out

    # ---- planner output and analyst findings ----------------------------------
    def save_plan(self, plan: dict) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO plans (id, created, data) VALUES (?,?,?)"
                " ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                (plan["id"], plan.get("created") or time.time(), json.dumps(plan)),
            )

    def get_plan(self, plan_id: str) -> Optional[dict]:
        with self._conn() as c:
            row = c.execute("SELECT data FROM plans WHERE id=?", (plan_id,)).fetchone()
        return json.loads(row["data"]) if row else None

    def list_plans(self, limit: int = 20) -> list[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT data FROM plans ORDER BY created DESC LIMIT ?", (limit,)).fetchall()
        return [json.loads(r["data"]) for r in rows]

    def latest_plan(self) -> Optional[dict]:
        plans = self.list_plans(1)
        return plans[0] if plans else None

    def save_analysis(self, run_id: str, analysis: dict) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO analyses (run_id, created, data) VALUES (?,?,?)"
                " ON CONFLICT(run_id) DO UPDATE SET data=excluded.data, created=excluded.created",
                (run_id, time.time(), json.dumps(analysis)),
            )

    def get_analysis(self, run_id: str) -> Optional[dict]:
        with self._conn() as c:
            row = c.execute("SELECT data FROM analyses WHERE run_id=?", (run_id,)).fetchone()
        return json.loads(row["data"]) if row else None

    def list_analyses(self) -> list[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT data FROM analyses ORDER BY created").fetchall()
        return [json.loads(r["data"]) for r in rows]

    # ---- key/value state (active run marker, blocked flag, kill request) ------
    def kv_get(self, key: str) -> Any:
        with self._conn() as c:
            row = c.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else None

    def kv_set(self, key: str, value: Any) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO kv (key, value, updated) VALUES (?,?,?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated=excluded.updated",
                (key, json.dumps(value), time.time()),
            )

    def kv_claim(self, key: str, value: Any, stale_after: float) -> bool:
        """Atomically take key unless another live holder refreshed it within
        stale_after seconds. Used as the cross-process experiment lock."""
        now = time.time()
        with self._conn() as c:
            c.execute(
                "INSERT INTO kv (key, value, updated) VALUES (?,?,?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated=excluded.updated"
                " WHERE kv.updated < ?",
                (key, json.dumps(value), now, now - stale_after),
            )
            row = c.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row is not None and json.loads(row["value"]) == value

    def kv_touch(self, key: str) -> None:
        with self._conn() as c:
            c.execute("UPDATE kv SET updated=? WHERE key=?", (time.time(), key))

    def kv_age(self, key: str) -> Optional[float]:
        with self._conn() as c:
            row = c.execute("SELECT updated FROM kv WHERE key=?", (key,)).fetchone()
        return (time.time() - row["updated"]) if row else None

    def kv_delete(self, key: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM kv WHERE key=?", (key,))


def _decode_run(row: sqlite3.Row) -> dict:
    d = dict(row)
    for k in RUN_JSON_FIELDS:
        if d.get(k):
            d[k] = json.loads(d[k])
    return d
