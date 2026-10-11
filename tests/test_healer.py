"""Self-healing agent: detection, diagnosis contract, runbooks and the full
LangGraph loop against the fake sandbox."""
import json
import time

import pytest
from fastapi.testclient import TestClient

from chaos import api as api_mod
from chaos import detection, runbooks
from chaos.healer import Healer, InvalidDiagnosis, validate_diagnosis
from chaos.llm import LLMError
from chaos.models import ExperimentConfig
from tests.fakes import IDS

AGENT_KW = {"tick_s": 0.1, "learn_s": 0.4, "verify_timeout_s": 3.0, "window_s": 1.0, "evidence_wait_s": 1.0}


def s(probe, ok, ts, latency=50.0, detail=""):
    return {"probe": probe, "ok": ok, "ts": ts, "latency_ms": latency, "detail": detail, "phase": "x"}


def h(service, state, ts, health="healthy"):
    return {"service": service, "state": state, "health": health, "ts": ts}


REF = detection.learn_reference([s(p, True, 1.0 + i, 100.0) for p in detection.PROBES for i in range(5)])


def healthy_containers(now):
    return [h(x, "running", now - 0.5) for x in ("webserver", "db", "broker")]


# ---------------------------------------------------------------- detection
def test_detects_read_path_failure_and_keeps_normals():
    now = 100.0
    rows = [s("api_documents", False, now - i, 5000, "timeout after 5.0s (ReadTimeout)") for i in range(3)]
    rows += [s("web_root", False, now - i, 5000, "timeout") for i in range(3)]
    rows += [s("api_status", True, now - 1, 120), s("doc_upload", True, now - 2, 110)]
    snap = detection.detect(rows, healthy_containers(now), REF, now, window_s=10)
    kinds = {(a["kind"], a["subject"]) for a in snap["anomalies"]}
    assert ("error_rate", "api_documents") in kinds and ("error_rate", "web_root") in kinds
    assert {n["subject"] for n in snap["normals"]} >= {"api_status", "doc_upload", "db", "broker"}
    assert all(a["id"].startswith("A") for a in snap["anomalies"])
    assert "timeout" in next(a for a in snap["anomalies"] if a["subject"] == "api_documents")["signal"]


def test_single_blip_is_not_an_anomaly():
    now = 100.0
    rows = [s("api_status", False, now - 1, 5000, "timeout")] + [s("api_documents", True, now - i) for i in range(3)]
    assert detection.detect(rows, healthy_containers(now), REF, now, window_s=10)["anomalies"] == []


def test_paused_container_and_status_report():
    now = 100.0
    rows = [s("api_status", False, now - 1, 300, "db=OK redis=ERROR celery=OK"),
            s("api_status", False, now - 3, 300, "db=OK redis=ERROR celery=OK")]
    health = [h("db", "running", now - 0.5), h("broker", "paused", now - 0.5)]
    snap = detection.detect(rows, health, REF, now, window_s=10)
    kinds = {(a["kind"], a["subject"]) for a in snap["anomalies"]}
    assert ("container_state", "broker") in kinds and ("dependency_report", "broker") in kinds


def test_a_timeout_is_an_error_not_a_latency_anomaly():
    now = 100.0
    rows = [s("web_root", True, now - 2), s("web_root", True, now - 1), s("web_root", False, now, 5000, "timeout")]
    snap = detection.detect(rows, healthy_containers(now), REF, now, window_s=10)
    assert not [a for a in snap["anomalies"] if a["kind"] == "latency"]


def test_latency_anomaly():
    now = 100.0
    rows = [s("api_documents", True, now - i, 2000) for i in range(3)]
    snap = detection.detect(rows, healthy_containers(now), REF, now, window_s=10)
    a = snap["anomalies"][0]
    assert a["kind"] == "latency" and a["subject"] == "api_documents" and a["deviation"] == "20.0x"


# ---------------------------------------------------------------- rules diagnosis
def _snap(rows, health, now=100.0):
    return detection.detect(rows, health, REF, now, window_s=10)


def test_rules_paused_db_is_unavailable():
    now = 100.0
    rows = [s("api_documents", False, now - i, 5000, "timeout") for i in range(3)]
    snap = _snap(rows, [h("db", "paused", now - 0.5), h("broker", "running", now - 0.5)])
    d = detection.rules_diagnosis(snap)
    assert d["failure_class"] == "db_unavailable"
    validate_diagnosis(d, snap, [])   # the rules output obeys the same contract as the AI


def test_rules_broker_slow_when_reads_ok_and_containers_running():
    now = 100.0
    rows = [s("api_status", False, now - i * 2, 5000, "timeout") for i in range(3)]
    rows += [s("doc_upload", False, now - i * 3, 5000, "timeout") for i in range(2)]
    rows += [s("api_documents", True, now - i) for i in range(3)] + [s("web_root", True, now - i) for i in range(3)]
    snap = _snap(rows, healthy_containers(now))
    d = detection.rules_diagnosis(snap)
    assert d["failure_class"] == "broker_slow"
    cited = {c for st in d["reasoning"] for c in st["cites"]}
    assert any(c.startswith("N") for c in cited)   # "reads are normal" is part of the argument
    assert detection.rules_diagnosis(snap, excluded=["broker_slow"])["failure_class"] == "unknown"


def test_expected_class_mapping():
    assert detection.expected_class("dependency_pause", "db") == "db_unavailable"
    assert detection.expected_class("network_latency", "broker") == "broker_slow"
    assert detection.expected_class("dependency_pause", "webserver") is None


# ---------------------------------------------------------------- AI contract
def _good(snap, cls="db_unavailable"):
    return {"failure_class": cls, "confidence": "high", "summary": "db frozen",
            "reasoning": [{"step": "db paused", "cites": [snap["anomalies"][0]["id"]]}], "ruled_out": []}


@pytest.fixture
def db_snap():
    now = 100.0
    rows = [s("api_documents", False, now - i, 5000, "timeout") for i in range(3)]
    return _snap(rows, [h("db", "paused", now - 0.5), h("broker", "running", now - 0.5)])


def test_validate_accepts_grounded_diagnosis(db_snap):
    assert validate_diagnosis(_good(db_snap), db_snap, [])["failure_class"] == "db_unavailable"


@pytest.mark.parametrize("mutate,msg", [
    (lambda d: d["reasoning"][0].update(cites=["A99"]), "do not exist"),
    (lambda d: d["reasoning"][0].update(cites=[]), "must cite"),
    (lambda d: d["reasoning"][0].update(cites=["N1"]), "at least one anomaly"),
    (lambda d: d.update(failure_class="restart_everything"), "failure_class"),
    (lambda d: d.update(command="docker rm -f db"), "command"),
])
def test_validate_rejects_ungrounded_or_unsafe(db_snap, mutate, msg):
    d = _good(db_snap)
    mutate(d)
    with pytest.raises(InvalidDiagnosis, match=msg):
        validate_diagnosis(d, db_snap, [])


def test_validate_rejects_excluded_class(db_snap):
    with pytest.raises(InvalidDiagnosis, match="excluded"):
        validate_diagnosis(_good(db_snap), db_snap, ["db_unavailable"])


# ---------------------------------------------------------------- runbooks
def test_every_class_has_exactly_one_runbook():
    assert set(runbooks.RUNBOOKS) == set(detection.FAILURE_CLASSES)
    assert runbooks.runbook_for("anything else")["id"] == "RB-0"


def test_runbook_unpause(docker):
    docker.state["db"]["paused"] = True
    res = runbooks.execute(docker, runbooks.runbook_for("db_unavailable"))
    assert res["ok"] and not docker.paused("db")
    assert runbooks.target_check(docker, runbooks.runbook_for("db_unavailable"))[0]


def test_runbook_clear_shaping(docker):
    docker.state["broker"]["netem"] = True
    res = runbooks.execute(docker, runbooks.runbook_for("broker_slow"))
    assert res["ok"] and not docker.state["broker"]["netem"]
    assert ("netem_clear", IDS["broker"]) in docker.calls


def test_runbook_unknown_escalates_without_touching_anything(docker):
    res = runbooks.execute(docker, runbooks.runbook_for("unknown"))
    assert res.get("escalated") and not res["ok"] and docker.calls == []


def test_runbook_refuses_non_allowlisted_service(docker):
    rb = {**runbooks.runbook_for("db_unavailable"), "service": "webserver"}
    res = runbooks.execute(docker, rb)
    assert not res["ok"] and docker.calls == []


# ---------------------------------------------------------------- the full loop
class ScriptedProvider:
    name, model = "fake", "fake-1"

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = 0

    def complete_json(self, system, user):
        self.calls += 1
        sig = json.loads(user.split("\n", 1)[1].split("\n\nYour previous")[0])
        ans = self.answers.pop(0) if self.answers else "unknown"
        if isinstance(ans, Exception):
            raise ans
        return {"failure_class": ans, "confidence": "high", "summary": "scripted",
                "reasoning": [{"step": "scripted", "cites": [sig["anomalies"][0]["id"]]}], "ruled_out": []}


def _attach(engine, store, docker, provider=None):
    healer = Healer(store, docker, lambda: provider, notify=engine.agent_remediated, **AGENT_KW)
    engine.on_start = healer.watch
    return healer


def _run(engine, healer, store, target="db", duration=8):
    rid = engine.start(ExperimentConfig(target=target, duration_s=duration, remediation="agent"))
    engine.wait(40)
    healer.wait(15)
    return rid, store.get_incident(f"inc-{rid}"), store.get_run(rid)


def test_agent_detects_and_fixes_db_pause_with_rules(engine, store, docker, monkeypatch):
    healer = _attach(engine, store, docker)
    # blindness: the agent must never read the run row (config, fault spec)
    real_get_run = store.get_run
    monkeypatch.setattr(store, "get_run", lambda *a: (_ for _ in ()).throw(AssertionError("agent read the run")))
    rid = engine.start(ExperimentConfig(target="db", duration_s=8, remediation="agent"))
    engine.wait(40)
    healer.wait(15)
    monkeypatch.setattr(store, "get_run", real_get_run)
    inc, run = store.get_incident(f"inc-{rid}"), store.get_run(rid)
    assert inc["status"] == "resolved", inc.get("end_reason")
    att = inc["attempts"][0]
    assert att["diagnosis"]["failure_class"] == "db_unavailable" and att["source"]["mode"] == "rules"
    assert att["runbook"]["id"] == "RB-1" and att["execution"]["ok"] and att["verification"]["ok"]
    assert run["outcome"]["result"] == "remediated_by_agent"
    assert run["outcome"]["remediation"]["remediated_by"] == "agent"
    assert run["fault_end"] - run["fault_start"] < 6           # fixed well before the 8 s deadline
    assert inc["detected_at"] >= run["fault_start"]           # no detection before the fault existed
    assert [t["node"] for t in inc["trace"]][-1] == "finalize"
    assert not docker.paused("db")


def test_wrong_first_diagnosis_is_caught_by_verify_and_retried(engine, store, docker):
    provider = ScriptedProvider(["broker_unavailable", "db_unavailable"])
    healer = _attach(engine, store, docker, provider)
    rid, inc, run = _run(engine, healer, store, duration=15)
    assert inc["status"] == "resolved"
    first, second = inc["attempts"]
    assert first["runbook"]["id"] == "RB-2" and not first["verification"]["ok"]
    assert first["rules_check"] == {"failure_class": "db_unavailable", "agrees": False}
    assert second["diagnosis"]["failure_class"] == "db_unavailable" and second["source"]["mode"] == "llm"
    kinds = [e["kind"] for e in store.events(rid)]
    assert "agent_fix_ineffective" in kinds and "agent_remediated" in kinds
    assert run["outcome"]["result"] == "remediated_by_agent"


def test_provider_failure_falls_back_to_rules(engine, store, docker):
    healer = _attach(engine, store, docker, ScriptedProvider([LLMError("openai: HTTP 503")]))
    _, inc, _ = _run(engine, healer, store)
    att = inc["attempts"][0]
    assert inc["status"] == "resolved" and att["source"]["mode"] == "rules-fallback"
    assert "503" in att["source"]["error"]


def test_unknown_escalates_and_engine_deadline_still_removes_fault(engine, store, docker):
    healer = _attach(engine, store, docker, ScriptedProvider(["unknown"]))
    _, inc, run = _run(engine, healer, store, duration=3)
    assert inc["status"] == "escalated"
    assert inc["attempts"][0]["runbook"]["id"] == "RB-0"
    assert run["outcome"]["remediation"]["remediated_by"] == "engine at the planned end"
    assert run["outcome"]["fault_removal"]["verified"] and not docker.paused("db")


def test_no_fault_means_no_incident(store, docker, engine):
    healer = Healer(store, docker, lambda: None, **AGENT_KW)
    store.create_run("baseline-x", "experiment", "baseline", {})
    healer.watch("baseline-x")
    time.sleep(1.5)
    store.update_run("baseline-x", state="completed")
    healer.wait(5)
    assert store.get_incident("inc-baseline-x")["status"] == "no_incident"


def test_api_agent_mode_toggle_and_grading(store, docker, engine):
    app = api_mod.create_app(store=store, docker=docker, engine=engine, start_monitor=False, provider=None,
                             healer_kwargs=AGENT_KW, agent_min_duration_s=8, agent_default=True)
    with TestClient(app) as c:
        assert c.get("/api/agent").json()["enabled"] is True
        r = c.post("/api/experiments", json={"target": "db", "duration_s": 2}).json()
        assert r["ok"]
        cfg = store.get_run(r["run_id"])["config"]
        assert cfg["remediation"] == "agent" and cfg["duration_s"] == 8   # held long enough for the agent
        engine.wait(40)
        app.state.healer.wait(15)
        d = c.get("/api/incidents/latest").json()
        g = d["grade"]
        assert d["incident"]["status"] == "resolved"
        assert g["expected_class"] == "db_unavailable" and g["correct"] is True
        assert g["remediated_by"] == "agent" and g["time_to_detect_s"] is not None
        assert c.get("/api/incidents").json()[0]["grade"]["correct"] is True
        assert c.post("/api/agent", json={"enabled": False}).json()["enabled"] is False
        assert c.post("/api/agent", json={"enabled": False, "x": 1}).status_code == 422
