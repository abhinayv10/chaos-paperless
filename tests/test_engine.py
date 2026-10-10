"""Experiment lifecycle, automatic abort, kill switch, rollback and blocking."""
import threading
import time

import pytest

from chaos.engine import Engine, ExperimentRejected
from chaos.faults import PauseFault, emergency_rollback
from chaos.models import ExperimentConfig, Thresholds
from chaos.safety import SafetyState
from tests.conftest import FAST_TIMINGS, FAST_WORKLOAD, TEST_LIMITS
from tests.fakes import IDS, FakeClient

LENIENT = Thresholds(max_error_rate=1.0, max_p95_latency_ms=30000, window_s=2, min_samples=3)
STRICT = Thresholds(max_error_rate=0.3, max_p95_latency_ms=30000, window_s=2, min_samples=3)


def run(engine, timeout=30, **kw):
    cfg = ExperimentConfig(**{"target": "db", "duration_s": 2, "recovery_timeout_s": 10,
                              "thresholds": LENIENT, **kw})
    run_id = engine.start(cfg)
    engine.wait(timeout)
    assert not engine.running_here(), "experiment thread did not finish"
    return engine.store.get_run(run_id)


def test_completed_experiment_injects_observes_removes_and_recovers(engine, docker):
    r = run(engine)
    out = r["outcome"]
    assert r["state"] == "completed" and out["result"] == "completed"
    assert ("pause", IDS["db"]) in docker.calls and ("unpause", IDS["db"]) in docker.calls
    assert not docker.paused("db")
    assert out["fault_removal"]["verified"] is True
    assert out["recovery"]["verified"] is True and out["recovery"]["recovery_time_s"] is not None
    assert r["fault_start"] and r["fault_end"] and r["fault_end"] > r["fault_start"]
    assert out["impact"]["impact_detected"] is True  # db pause breaks the fake app
    metrics = r["summary"]["metrics"]
    assert metrics["baseline"]["critical"]["error_rate"] == 0
    assert metrics["fault"]["critical"]["error_rate"] > 0.5
    assert engine.safety.active() is None and engine.safety.blocked() is None
    assert not engine.busy()


def test_threshold_breach_triggers_automatic_abort_and_rollback(engine, docker):
    t0 = time.time()
    r = run(engine, duration_s=20, thresholds=STRICT)
    out = r["outcome"]
    assert r["state"] == "aborted" and out["result"] == "aborted_threshold"
    assert out["breaches"] and out["breaches"][0]["metric"] == "error_rate"
    assert r["fault_end"] - r["fault_start"] < 10, "abort must happen long before the 20s planned end"
    assert time.time() - t0 < 20
    assert not docker.paused("db")
    assert out["fault_removal"]["verified"] and out["recovery"]["verified"]
    kinds = [e["kind"] for e in engine.store.events(r["id"])]
    assert "threshold_breach" in kinds and "fault_removed" in kinds


def test_kill_switch_aborts_and_removes_fault_immediately(engine, docker):
    cfg = ExperimentConfig(target="db", duration_s=30, thresholds=LENIENT, recovery_timeout_s=10)
    run_id = engine.start(cfg)
    deadline = time.time() + 10
    while not docker.paused("db") and time.time() < deadline:
        time.sleep(0.05)
    assert docker.paused("db")
    res = engine.kill("test kill")
    assert res["rollback_verified"] is True
    assert not docker.paused("db"), "kill switch must remove the fault without waiting for the run thread"
    engine.wait(20)
    r = engine.store.get_run(run_id)
    assert r["state"] == "aborted" and "kill" in r["outcome"]["abort_reason"]
    assert r["fault_end"] - r["fault_start"] < 10


def test_kill_request_from_another_process_is_honoured(engine, store, docker):
    cfg = ExperimentConfig(target="db", duration_s=30, thresholds=LENIENT, recovery_timeout_s=10)
    run_id = engine.start(cfg)
    while not docker.paused("db"):
        time.sleep(0.05)
    SafetyState(store).request_kill("cli")  # what `python -m chaos kill` does
    engine.wait(20)
    r = store.get_run(run_id)
    assert r["state"] == "aborted" and not docker.paused("db")


def test_unverifiable_removal_blocks_further_experiments(engine, docker):
    docker.fail_unpause = True
    r = run(engine)
    assert r["state"] == "failed" and r["outcome"]["result"] == "cleanup_or_recovery_unverified"
    assert r["outcome"]["fault_removal"]["verified"] is False
    assert engine.safety.blocked() is not None
    assert engine.safety.active() is not None, "marker kept so the fault can still be cleaned up"
    with pytest.raises(ExperimentRejected) as e:
        engine.start(ExperimentConfig(target="broker", duration_s=2))
    assert e.value.status == 423
    # unblock refuses while the fault is still present ...
    ok, _ = engine.unblock()
    assert not ok and engine.safety.blocked()
    # ... and succeeds once the environment is verifiably clean
    docker.fail_unpause = False
    ok, checks = engine.unblock()
    assert ok, [c for c in checks if not c.ok]
    assert not docker.paused("db") and engine.safety.blocked() is None


def test_injection_failure_still_runs_rollback(engine, docker):
    docker.fail_pause = True
    r = run(engine)
    assert r["state"] == "aborted"
    assert "error during fault window" in r["outcome"]["abort_reason"]
    assert ("unpause", IDS["db"]) in docker.calls or r["outcome"]["fault_removal"]["verified"]
    assert engine.safety.blocked() is None


def test_unrecovered_application_blocks(store, docker):
    class NeverRecovers(FakeClient):
        def probe_documents(self):
            r = super().probe_documents()
            if hasattr(self, "_broken"):
                r.ok = False
            return r

    clients = []

    def factory():
        c = NeverRecovers(docker)
        clients.append(c)
        return c

    eng = Engine(store, docker, factory, timings=FAST_TIMINGS, limits=TEST_LIMITS,
                 workload_kwargs=FAST_WORKLOAD, spawn_watchdog=False)
    cfg = ExperimentConfig(target="db", duration_s=1, thresholds=LENIENT, recovery_timeout_s=5)
    run_id = eng.start(cfg)
    while not docker.paused("db"):
        time.sleep(0.05)
    for c in clients:
        c._broken = True  # app stays broken after the fault is removed
    eng.wait(30)
    r = store.get_run(run_id)
    assert r["outcome"]["fault_removal"]["verified"] is True
    assert r["outcome"]["recovery"]["verified"] is False
    assert r["state"] == "failed" and eng.safety.blocked()


def test_overlapping_experiments_are_rejected(engine):
    cfg = ExperimentConfig(target="db", duration_s=3, thresholds=LENIENT)
    engine.start(cfg)
    with pytest.raises(ExperimentRejected) as e:
        engine.start(ExperimentConfig(target="broker", duration_s=2))
    assert e.value.status == 409
    engine.kill("cleanup")
    engine.wait(20)


def test_lock_is_exclusive_across_processes(store):
    a, b = SafetyState(store), SafetyState(store)
    assert a.try_acquire("run-a")
    assert not b.try_acquire("run-b"), "a second controller process must not start a run"
    assert b.busy()
    a.release()
    assert b.try_acquire("run-b")
    b.release()


def test_stale_lock_from_dead_process_expires(store):
    a = SafetyState(store)
    a.try_acquire("run-a")
    with store._conn() as c:
        c.execute("UPDATE kv SET updated=0 WHERE key='running_run'")
    b = SafetyState(store)
    assert b.try_acquire("run-b")
    b.release()


def test_unauthorised_target_never_reaches_docker(engine, docker):
    for target in ("webserver", "postgres-prod"):
        with pytest.raises(ExperimentRejected):
            engine.start(ExperimentConfig(target=target, duration_s=5))
    assert not [c for c in docker.calls if c[0] == "pause"]
    rejected = engine.store.list_runs("experiment")
    assert all(r["state"] == "rejected" for r in rejected) and len(rejected) == 2


def test_preflight_fails_when_dependency_unhealthy(engine, docker):
    docker.state["broker"]["health"] = "unhealthy"
    errors, checks, _ = engine.preflight(ExperimentConfig(target="db", duration_s=5))
    assert errors and any("broker" in e for e in errors)


def test_preflight_fails_when_target_already_paused(engine, docker):
    docker.state["db"]["paused"] = True
    errors, _, _ = engine.preflight(ExperimentConfig(target="db", duration_s=5))
    assert errors


def test_crash_recovery_rolls_back_leftover_fault(engine, store, docker):
    # simulate a controller that died after injecting
    docker.state["db"]["paused"] = True
    store.create_run("exp-dead", "experiment", "active", {"target": "db"})
    SafetyState(store).mark_active({"fault_type": "dependency_pause", "service": "db",
                                    "container_id": IDS["db"], "run_id": "exp-dead", "duration_s": 30})
    report = engine.recover_on_startup()
    assert report["rollback"]["verified"] is True
    assert not docker.paused("db")
    assert store.get_run("exp-dead")["state"] == "interrupted"
    assert SafetyState(store).active() is None


def test_crash_recovery_skips_when_another_controller_is_live(engine, store, docker):
    other = SafetyState(store)
    other.try_acquire("exp-live")  # held by a live process
    docker.state["db"]["paused"] = True
    store.create_run("exp-live", "experiment", "active", {})
    SafetyState(store).mark_active({"fault_type": "dependency_pause", "service": "db",
                                    "container_id": IDS["db"], "run_id": "exp-live"})
    fresh = Engine(store, docker, lambda: FakeClient(docker), spawn_watchdog=False)
    assert "skipped" in fresh.recover_on_startup()
    assert docker.paused("db"), "must not interfere with another live controller's run"
    other.release()


def test_cleanup_is_idempotent(docker):
    spec = {"fault_type": "dependency_pause", "service": "db", "container_id": IDS["db"], "run_id": "x"}
    f = PauseFault(docker, spec)
    f.remove()  # nothing applied yet
    f.inject()
    f.remove()
    f.remove()
    assert f.verify_removed()[0]
    ok, notes = emergency_rollback(docker, spec)
    ok2, _ = emergency_rollback(docker, spec)
    assert ok and ok2


def test_emergency_rollback_only_touches_allowlisted_sandbox_services(docker):
    docker.state["webserver"]["paused"] = True  # not in allowlist -> left alone
    docker.state["broker"]["paused"] = True
    ok, notes = emergency_rollback(docker)
    assert not docker.paused("broker")
    assert docker.paused("webserver")


def test_baseline_run_records_metrics(engine):
    rid = engine.start_baseline(10)
    time.sleep(1.0)
    engine.kill("shorten test")  # baseline honours the kill switch too
    engine.wait(40)
    r = engine.store.get_run(rid)
    assert r["state"] in ("aborted", "completed")
    assert r["summary"]["metrics"]["baseline"]["critical"]["n"] > 0
