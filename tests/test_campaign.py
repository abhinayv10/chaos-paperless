"""New fault types, the resilience scan and the LangGraph campaign
(scan -> rank -> select -> human confirmation -> tool-based injection -> report)."""
import json
import time

import pytest
from fastapi.testclient import TestClient

from chaos import api as api_mod
from chaos import campaign as camp
from chaos import config, detection, runbooks
from chaos.faults import build_fault, emergency_rollback
from chaos.models import ExperimentConfig
from chaos.safety import validate_config
from chaos.scanner import Scanner
from tests.conftest import TEST_LIMITS
from tests.fakes import IDS, FakeClient
from tests.test_healer import AGENT_KW


# ---------------------------------------------------------------- new faults
@pytest.mark.parametrize("cfg,msg", [
    (dict(fault_type="packet_loss"), "loss_pct is required"),
    (dict(fault_type="packet_loss", loss_pct=90), "outside"),
    (dict(fault_type="cpu_exhaustion"), "cpu_limit is required"),
    (dict(fault_type="cpu_exhaustion", cpu_limit=2.0), "outside"),
    (dict(fault_type="dependency_stop", loss_pct=10), "only valid for packet_loss"),
    (dict(fault_type="dependency_pause", cpu_limit=0.1), "only valid for cpu_exhaustion"),
])
def test_policy_for_new_fault_parameters(cfg, msg):
    errors = validate_config(ExperimentConfig(target="db", duration_s=20, **cfg))
    assert any(msg in e for e in errors), errors


def test_new_faults_are_allowed_on_dependencies_only():
    assert not validate_config(ExperimentConfig(target="broker", duration_s=20, fault_type="packet_loss", loss_pct=20))
    assert validate_config(ExperimentConfig(target="webserver", duration_s=20, fault_type="dependency_stop"))


def spec(fault, svc="db", **kw):
    return {"fault_type": fault, "service": svc, "container_id": IDS[svc], "run_id": "t", "duration_s": 10, **kw}


def test_stop_fault_inject_remove_verify(docker):
    f = build_fault(docker, spec("dependency_stop"))
    assert all(c.ok for c in f.preflight())
    f.inject()
    assert not docker.state["db"]["running"] and not f.verify_removed()[0]
    f.remove()
    assert docker.state["db"]["running"] and f.verify_removed()[0]


def test_cpu_fault_inject_remove_verify(docker):
    f = build_fault(docker, spec("cpu_exhaustion", cpu_limit=0.05))
    f.inject()
    assert docker.inspect(IDS["db"]).cpu_limit == pytest.approx(0.05) and not f.verify_removed()[0]
    f.remove()
    assert docker.inspect(IDS["db"]).cpu_limit == 0 and f.verify_removed()[0]


def test_emergency_sweep_restarts_stopped_and_lifts_cpu_quota(docker):
    docker.state["db"]["running"] = False
    docker.state["broker"]["cpus"] = 0.05
    ok, notes = emergency_rollback(docker)
    assert ok and docker.state["db"]["running"] and docker.state["broker"]["cpus"] == 0
    assert "started db" in notes and "removed CPU quota on broker" in notes


def test_cpu_saturation_signal_and_rules_class():
    now = 100.0
    ref = detection.learn_reference([{"probe": p, "ok": True, "ts": 1.0 + i, "latency_ms": 100.0, "detail": ""}
                                     for p in detection.PROBES for i in range(5)])
    rows = [{"probe": "api_documents", "ok": False, "ts": now - i, "latency_ms": 5000, "detail": "timeout"}
            for i in range(3)]
    health = [{"service": s, "state": "running", "health": "healthy", "ts": now} for s in ("db", "broker")]
    snap = detection.detect(rows, health, ref, now, 10, resources={"db": {"cpu_pct": 4.9, "cpu_limit": 0.05},
                                                                    "broker": {"cpu_pct": 1.0, "cpu_limit": 0}})
    assert any(a["kind"] == "cpu_saturation" and a["subject"] == "db" for a in snap["anomalies"])
    assert detection.rules_diagnosis(snap)["failure_class"] == "db_cpu_starved"
    assert detection.expected_class("cpu_exhaustion", "db") == "db_cpu_starved"
    assert detection.expected_class("dependency_stop", "broker") == "broker_unavailable"
    assert detection.expected_class("packet_loss", "db") == "db_slow"


def test_runbooks_for_stop_and_cpu(docker):
    docker.state["broker"]["running"] = False
    assert runbooks.execute(docker, runbooks.runbook_for("broker_unavailable"))["ok"]
    assert docker.state["broker"]["running"] and ("start", IDS["broker"]) in docker.calls
    docker.state["db"]["cpus"] = 0.05
    res = runbooks.execute(docker, runbooks.runbook_for("db_cpu_starved"))
    assert res["ok"] and docker.state["db"]["cpus"] == 0
    assert runbooks.target_check(docker, runbooks.runbook_for("db_cpu_starved"))[0]


# ---------------------------------------------------------------- scan
@pytest.fixture
def scan_result(docker):
    steps = []
    res = Scanner(docker, lambda: FakeClient(docker), emit=steps.append, rounds=2).scan()
    return res, steps


def test_scan_measures_and_finds_weak_points(scan_result):
    res, steps = scan_result
    cats = {f["category"] for f in res["findings"]}
    assert {"single_point_of_failure", "no_self_healing", "resource_isolation", "circuit_breaker"} <= cats
    assert res["probes"]["api_documents"]["n"] == 2 and res["steps"][-1]["title"] == "Scan complete"
    assert any(s["status"] == "running" for s in steps)            # progress is streamed
    for f in res["findings"]:
        assert f["evidence"] and f["tests"]
        for t in f["tests"]:                                      # every suggestion is a valid experiment
            assert not validate_config(camp.to_config({**t, "title": "x", "expected_effect": "y"}, 30))


def test_rank_validation_grounds_proposals_in_the_scan(scan_result):
    findings = scan_result[0]["findings"]
    good = camp.rules_rank(findings)
    reply, rejected = camp.validate_rank(good, findings, 30, config.LIMITS)
    assert len(reply["selection"]) >= camp.MIN_SELECT and not rejected
    bad = json.loads(json.dumps(good))
    bad["selection"][0]["fault_type"], bad["selection"][0]["target"] = "packet_loss", "broker"
    bad["selection"][0]["finding_id"] = next(f["id"] for f in findings if f["category"] == "single_point_of_failure")
    bad["selection"][0]["loss_pct"] = 20
    _, rejected = camp.validate_rank(bad, findings, 30, config.LIMITS) if len(bad["selection"]) > 3 else (None, [1])
    assert rejected
    with pytest.raises(camp.LLMError, match="every finding"):
        camp.validate_rank({**good, "ranking": good["ranking"][:1]}, findings, 30, config.LIMITS)
    with pytest.raises(camp.LLMError):
        camp.validate_rank({**good, "selection": good["selection"][:2]}, findings, 30, config.LIMITS)


# ---------------------------------------------------------------- full campaign
class CampaignProvider:
    """Ranks and selects via the 'AI', then tries one unapproved tool call."""
    name, model = "fake", "fake-1"

    def __init__(self):
        self.exec_calls = 0

    def complete_json(self, system, user):
        if "planning step" in system:
            f = json.loads(user.split("\n", 1)[1])["findings"]
            r = camp.rules_rank(f)
            r["ranking"].reverse()
            for i, x in enumerate(r["ranking"], 1):
                x["rank"] = i
            return r
        if "execution agent" in system:
            self.exec_calls += 1
            items = json.loads(user.split("\n", 1)[1])
            if self.exec_calls == 1:   # tries to inject something different from what was approved
                x = items[0]
                return {"tool": "inject_fault", "args": {**{k: x[k] for k in ("item_id", "fault_type", "target")},
                                                         "latency_ms": None, "loss_pct": None, "cpu_limit": 0.5},
                        "reason": "more aggressive"}
            x = next(i for i in items if i["status"] == "pending")
            return {"tool": "inject_fault", "args": {k: x[k] for k in ("item_id", "fault_type", "target", "latency_ms",
                                                                       "loss_pct", "cpu_limit")}, "reason": "next"}
        return {"failure_class": "unknown", "confidence": "low", "summary": "x", "reasoning": [{"step": "x", "cites": []}]}


def _app(store, docker, engine, provider=None):
    return api_mod.create_app(store=store, docker=docker, engine=engine, start_monitor=False, provider=provider,
                              healer_kwargs=AGENT_KW, agent_min_duration_s=8, scan_rounds=1, settle_s=0.05)


def _until(c, cid, statuses, timeout=120):
    end = time.time() + timeout
    while time.time() < end:
        d = c.get(f"/api/campaign/{cid}").json()["campaign"]
        if d["status"] in statuses:
            return d
        time.sleep(0.2)
    raise AssertionError(f"campaign stuck in {d['status']}")


def test_campaign_waits_for_human_and_runs_only_what_was_approved(store, docker, engine):
    engine.limits = TEST_LIMITS
    with TestClient(_app(store, docker, engine)) as c:
        cid = c.post("/api/campaign").json()["id"]
        assert c.post("/api/campaign").status_code == 409          # one campaign at a time
        d = _until(c, cid, {"awaiting_confirmation"})
        assert d["scan"]["findings"] and d["ranking"]["source"]["mode"] == "rules"
        items = [x["id"] for x in d["selection"]["items"]]
        assert len(items) >= 3 and store.list_runs("experiment") == []   # nothing injected before confirmation
        assert c.post(f"/api/campaign/{cid}/confirm", json={"approved": items[:2]}).status_code == 400
        assert c.post(f"/api/campaign/{cid}/confirm", json={"approved": ["E99", *items[:3]]}).status_code == 400
        assert c.post(f"/api/campaign/{cid}/confirm", json={"approved": items[:3]}).json()["ok"]
        d = _until(c, cid, {"done", "failed", "stopped"}, timeout=240)
        assert d["status"] == "done", d.get("error")
        done = [x for x in d["selection"]["items"] if x["status"] == "done"]
        assert [x["id"] for x in done] == items[:3]
        assert all(x["status"] == "declined" for x in d["selection"]["items"][3:])
        runs = store.list_runs("experiment")
        assert len(runs) == 3 and all(r["config"]["remediation"] == "agent" for r in runs)
        for x in done:
            r = x["result"]
            assert x["tool_call"]["tool"] == "inject_fault" and x["tool_call"]["result"]["ok"]
            assert r["phases"]["before"]["n"] and r["phases"]["during"]["n"]
            assert r["agent"]["status"] == "resolved" and r["agent"]["correct"] is True
            assert r["agent"]["remediated_by"] == "agent"
        assert d["report"]["experiments"] == 3 and d["report"]["self_healed"] == 3
        assert d["report"]["findings"] and [t["node"] for t in d["trace"]][-1] == "report"


def test_ai_tool_call_outside_the_approval_is_refused(store, docker, engine):
    engine.limits = TEST_LIMITS
    prov = CampaignProvider()
    with TestClient(_app(store, docker, engine, provider=prov)) as c:
        cid = c.post("/api/campaign").json()["id"]
        d = _until(c, cid, {"awaiting_confirmation"})
        assert d["ranking"]["source"]["mode"] == "llm"
        ids = [x["id"] for x in d["selection"]["items"]][:3]
        c.post(f"/api/campaign/{cid}/confirm", json={"approved": ids})
        d = _until(c, cid, {"done", "failed", "stopped"}, timeout=240)
        first = next(x for x in d["selection"]["items"] if x["id"] == ids[0])
        src = first["tool_call"]["source"]
        assert src["mode"] == "rules-fallback" and "refused" in src["error"]
        assert all(r["config"].get("cpu_limit") != 0.5 for r in store.list_runs("experiment"))
        later = [x for x in d["selection"]["items"] if x["id"] in ids[1:]]
        assert all(x["tool_call"]["source"]["mode"] == "llm" for x in later)


def test_cancel_at_confirmation_injects_nothing(store, docker, engine):
    with TestClient(_app(store, docker, engine)) as c:
        cid = c.post("/api/campaign").json()["id"]
        _until(c, cid, {"awaiting_confirmation"})
        assert c.post(f"/api/campaign/{cid}/cancel").json()["ok"]
        d = _until(c, cid, {"cancelled"})
        assert d["report"]["experiments"] == 0 and store.list_runs("experiment") == []


# ---------------------------------------------------------------- phase 2: connections, memory, disk
def test_firewall_memory_and_disk_faults(docker):
    fw = build_fault(docker, spec("connection_refused"))
    fw.inject()
    assert docker.state["db"]["fw"] and not fw.verify_removed()[0]
    fw.remove()
    assert fw.verify_removed()[0]
    mem = build_fault(docker, spec("memory_exhaustion", memory_mb=24))
    mem.inject()
    assert docker.inspect(IDS["db"]).memory == 24 * 2 ** 20 and not mem.verify_removed()[0]
    mem.remove()
    assert ("recreate", "db") in docker.calls and mem.verify_removed()[0]
    disk = build_fault(docker, {**spec("disk_exhaustion", svc="webserver")})
    assert all(c.ok for c in disk.preflight())
    disk.inject()
    assert docker.scratch_pct == 100 and not disk.verify_removed()[0]
    disk.remove()
    assert disk.verify_removed()[0]


def test_memory_and_disk_need_their_parameters_and_targets():
    assert any("memory_mb is required" in e for e in validate_config(
        ExperimentConfig(target="db", duration_s=20, fault_type="memory_exhaustion")))
    assert any("outside" in e for e in validate_config(
        ExperimentConfig(target="db", duration_s=20, fault_type="memory_exhaustion", memory_mb=2)))
    assert any("not permitted" in e for e in validate_config(
        ExperimentConfig(target="db", duration_s=20, fault_type="disk_exhaustion")))


def test_sweep_clears_firewall_memory_and_disk_but_never_touches_the_webserver(docker):
    docker.state["broker"]["fw"] = True
    docker.state["db"]["mem"] = 24
    docker.state["webserver"]["paused"] = True     # not something the lab ever does: must be left alone
    docker.scratch_pct = 100.0
    docker.helper_image = True
    ok, notes = emergency_rollback(docker)
    assert not docker.state["broker"]["fw"] and docker.state["db"]["mem"] == 0 and docker.scratch_pct == 0
    assert docker.state["webserver"]["paused"] and not [c for c in docker.calls if c == ("unpause", IDS["webserver"])]
    assert "freed the scratch volume" in notes


def test_detection_tells_refused_from_hung_and_spots_memory_and_disk():
    now = 100.0
    ref = detection.learn_reference([{"probe": p, "ok": True, "ts": 1.0 + i, "latency_ms": 100.0, "detail": ""}
                                     for p in detection.PROBES for i in range(5)])
    health = [{"service": s, "state": "running", "health": "healthy", "ts": now} for s in ("db", "broker", "webserver")]
    fast = [{"probe": "api_documents", "ok": False, "ts": now - i, "latency_ms": 60, "detail": "500"} for i in range(3)]
    slow = [{**r, "latency_ms": 5000, "detail": "timeout"} for r in fast]
    assert detection.rules_diagnosis(detection.detect(fast, health, ref, now, 10))["failure_class"] == \
        "db_refusing_connections"
    assert detection.rules_diagnosis(detection.detect(slow, health, ref, now, 10))["failure_class"] == "db_slow"
    mem = detection.detect(fast, health, ref, now, 10, resources={"broker": {"memory_mb": 6, "mem_pct": 99,
                                                                             "oom_killed": True, "restart_count": 2}})
    assert detection.rules_diagnosis(mem)["failure_class"] == "broker_memory_exhausted"
    up = [{"probe": "doc_upload", "ok": False, "ts": now - i, "latency_ms": 80, "detail": "500"} for i in range(3)]
    disk = detection.detect(up, health, ref, now, 10, resources={"webserver": {"scratch_pct": 100, "scratch_mb": 64}})
    assert detection.rules_diagnosis(disk)["failure_class"] == "webserver_disk_full"
    assert detection.expected_class("disk_exhaustion", "webserver") == "webserver_disk_full"
    assert detection.expected_class("connection_refused", "broker") == "broker_refusing_connections"


def test_runbook_actions_are_bound_to_permitted_services(docker):
    rb = runbooks.runbook_for("webserver_disk_full")
    assert runbooks.execute(docker, {**rb, "service": "db"})["ok"] is False     # clear_disk is not a db action
    assert runbooks.execute(docker, {**runbooks.runbook_for("db_memory_exhausted"), "service": "webserver"})["ok"] is False
    docker.scratch_pct = 100.0
    assert runbooks.execute(docker, rb)["ok"] and docker.scratch_pct == 0


@pytest.mark.parametrize("cfg,cls", [
    (dict(target="db", fault_type="connection_refused"), "db_refusing_connections"),
    (dict(target="webserver", fault_type="disk_exhaustion"), "webserver_disk_full"),
    (dict(target="broker", fault_type="memory_exhaustion", memory_mb=6), "broker_memory_exhausted"),
])
def test_agent_heals_phase2_faults(engine, store, docker, cfg, cls):
    from chaos.healer import Healer
    engine.limits = TEST_LIMITS
    docker.helper_image = True
    healer = Healer(store, docker, lambda: None, notify=engine.agent_remediated, **AGENT_KW)
    engine.on_start = healer.watch
    rid = engine.start(ExperimentConfig(duration_s=8, remediation="agent", **cfg))
    engine.wait(40)
    healer.wait(15)
    inc, run = store.get_incident(f"inc-{rid}"), store.get_run(rid)
    assert inc["status"] == "resolved", (inc.get("end_reason"), [a["diagnosis"]["failure_class"] for a in inc["attempts"]])
    assert inc["attempts"][-1]["diagnosis"]["failure_class"] == cls
    assert run["outcome"]["remediation"]["remediated_by"] == "agent"


def test_every_finding_gets_an_experiment(scan_result):
    findings = scan_result[0]["findings"]
    reply, _ = camp.validate_rank(camp.rules_rank(findings), findings, 30, config.LIMITS)
    covered = {p["finding_id"] for p in reply["selection"]} | {c for p in reply["selection"] for c in p["also_covers"]}
    assert covered == {f["id"] for f in findings} and len(findings) >= 7
    # an AI selection that skips findings is topped up rather than leaving weak points untested
    partial = camp.rules_rank(findings)
    partial["selection"] = partial["selection"][:3]
    reply, _ = camp.validate_rank(partial, findings, 30, config.LIMITS)
    covered = {p["finding_id"] for p in reply["selection"]} | {c for p in reply["selection"] for c in p["also_covers"]}
    assert covered == {f["id"] for f in findings} and reply["coverage_added"]
