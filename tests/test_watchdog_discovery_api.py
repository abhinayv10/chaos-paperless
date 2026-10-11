"""Watchdog dead-man's switch, dependency discovery and API validation."""
import time

from fastapi.testclient import TestClient

from chaos import api as api_mod
from chaos.discovery import build_graph
from chaos.watchdog import watch
from tests.fakes import IDS, FakeClient, FakeDocker

SPEC = {"fault_type": "dependency_pause", "service": "db", "container_id": IDS["db"],
        "run_id": "exp-wd", "duration_s": 1}


# ---------------------------------------------------------------- watchdog
def test_watchdog_removes_fault_when_controller_never_does(store, docker):
    store.create_run("exp-wd", "experiment", "active", {})
    docker.state["db"]["paused"] = True
    result = watch(store, docker, SPEC, deadline=time.time() + 0.3, poll_s=0.05)
    assert result == "watchdog removed fault"
    assert not docker.paused("db")
    assert any(e["kind"] == "watchdog_rollback" for e in store.events("exp-wd"))


def test_watchdog_stands_down_when_controller_removed_fault(store, docker):
    store.create_run("exp-wd", "experiment", "active", {})
    store.update_run("exp-wd", fault_end=time.time())
    docker.state["db"]["paused"] = True  # pretend; watchdog must not act
    assert watch(store, docker, SPEC, deadline=time.time() + 5, poll_s=0.05) == "controller removed fault"
    assert ("unpause", IDS["db"]) not in docker.calls


def test_watchdog_blocks_when_it_cannot_remove(store, docker):
    store.create_run("exp-wd", "experiment", "active", {})
    docker.state["db"]["paused"] = True
    docker.fail_unpause = True
    assert "FAILED" in watch(store, docker, SPEC, deadline=time.time(), poll_s=0.01)
    assert store.kv_get("blocked")


# ---------------------------------------------------------------- discovery
COMPOSE = {
    "name": "paperless-chaos",
    "services": {
        "broker": {"image": "valkey", "healthcheck": {"test": ["CMD", "valkey-cli", "ping"]}},
        "db": {"image": "postgres:18", "environment": {"POSTGRES_DB": "paperless", "PGDATA": "/var/lib/db"}},
        "webserver": {
            "image": "paperless",
            "depends_on": {"db": {"condition": "service_healthy"}, "broker": {"condition": "service_healthy"}},
            "ports": ["127.0.0.1:8000:8000"],
            "environment": {"PAPERLESS_REDIS": "redis://broker:6379", "PAPERLESS_DBHOST": "db"},
        },
    },
}


def edge(g, a, b):
    return next((e for e in g["edges"] if e["from"] == a and e["to"] == b), None)


def test_graph_has_declared_and_inferred_edges_from_real_config():
    d = FakeDocker()
    g = build_graph(COMPOSE, d.project_containers(), {})
    wdb = edge(g, "webserver", "db")
    assert set(wdb["kinds"]) == {"declared", "inferred"}
    assert any("PAPERLESS_DBHOST" in ev for ev in wdb["evidence"])
    assert "redis" in " ".join(edge(g, "webserver", "broker")["evidence"]).lower()
    assert edge(g, "host", "webserver")["kinds"] == ["inferred"]
    assert edge(g, "db", "webserver") is None
    assert edge(g, "db", "broker") is None, "paths like /var/lib/db must not create edges"


def test_graph_reflects_runtime_health_and_missing_services():
    d = FakeDocker()
    d.state["db"]["paused"] = True
    d.state["broker"]["health"] = "unhealthy"
    g = build_graph(COMPOSE, d.project_containers(), {})
    status = {n["id"]: n["status"] for n in g["nodes"]}
    assert status["db"] == "paused" and status["broker"] == "unhealthy" and status["webserver"] == "healthy"
    g2 = build_graph(COMPOSE, [c for c in d.project_containers() if c.service != "broker"], {})
    assert {n["id"]: n["status"] for n in g2["nodes"]}["broker"] == "missing"


def test_manual_corrections_add_and_suppress_edges():
    d = FakeDocker()
    ov = {"add_edges": [{"from": "broker", "to": "db", "note": "celery results"}],
          "remove_edges": [{"from": "host", "to": "webserver"}], "notes": {"db": "primary store"}}
    g = build_graph(COMPOSE, d.project_containers(), ov)
    assert edge(g, "broker", "db")["kinds"] == ["manual"]
    assert edge(g, "host", "webserver") is None
    assert g["suppressed_edges"][0]["to"] == "webserver"
    assert next(n for n in g["nodes"] if n["id"] == "db")["note"] == "primary store"


# ---------------------------------------------------------------- API
def make_client(store, docker, engine):
    app = api_mod.create_app(store=store, docker=docker, engine=engine, start_monitor=False)
    return TestClient(app)


def test_api_rejects_unapproved_target_and_extra_fields(store, docker, engine):
    with make_client(store, docker, engine) as c:
        r = c.post("/api/experiments", json={"target": "webserver", "duration_s": 10})
        assert r.status_code == 400 and "not permitted on 'webserver'" in r.json()["errors"][0]
        r = c.post("/api/experiments", json={"target": "db", "duration_s": 10, "command": "rm -rf /"})
        assert r.status_code == 422
        r = c.post("/api/experiments", json={"target": "db", "duration_s": 9999})
        assert r.status_code == 400
    assert not [x for x in docker.calls if x[0] == "pause"]


def test_api_preflight_catalog_graph_and_kill(store, docker, engine):
    with make_client(store, docker, engine) as c:
        pf = c.post("/api/experiments/preflight", json={"target": "db", "duration_s": 10}).json()
        assert pf["ok"], pf
        cat = c.get("/api/catalog").json()
        assert set(cat["targets"]) == {"db", "broker", "webserver"} and cat["targets"]["webserver"] == ["disk_exhaustion"]
        g = c.get("/api/graph").json()
        assert {n["id"] for n in g["nodes"]} >= {"webserver", "db", "broker"}
        docker.state["broker"]["paused"] = True
        k = c.post("/api/kill").json()
        assert k["rollback_verified"] and not docker.paused("broker")


def test_api_blocked_state_returns_423(store, docker, engine):
    engine.safety.block("test", None)
    with make_client(store, docker, engine) as c:
        r = c.post("/api/experiments", json={"target": "db", "duration_s": 10})
        assert r.status_code == 423


def test_api_metrics_summarises_recent_traffic(store, docker, engine):
    from chaos.models import ProbeResult
    import time as _t
    now = _t.time()
    for i in range(10):
        store.add_sample("monitor", "idle", ProbeResult("web_root", i != 3, 100.0 + i, 200, "", now - 60 + i))
    store.add_sample("exp-x", "fault", ProbeResult("doc_processing", True, 4000.0, None, "", now - 30))
    with make_client(store, docker, engine) as c:
        m = c.get("/api/metrics").json()
    assert m["requests"]["total"] == 10 and m["requests"]["failed"] == 1
    assert abs(m["requests"]["error_rate"] - 0.1) < 1e-9 and len(m["trend"]["rpm"]) == 12
    assert m["processing"]["median_ms"] == 4000.0 and m["processing"]["count"] == 1
