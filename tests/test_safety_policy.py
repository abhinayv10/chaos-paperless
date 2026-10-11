"""Safety policy: experiment validation and threshold evaluation."""
import pytest
from pydantic import ValidationError

from chaos import config
from chaos.models import ExperimentConfig, Thresholds
from chaos.safety import evaluate_thresholds, validate_config


def cfg(**kw):
    base = {"target": "db", "duration_s": 30}
    base.update(kw)
    return ExperimentConfig(**base)


def test_valid_pause_config_is_allowed():
    assert validate_config(cfg()) == []
    assert validate_config(cfg(target="broker")) == []


def test_valid_latency_config_is_allowed():
    assert validate_config(cfg(fault_type="network_latency", latency_ms=200)) == []


@pytest.mark.parametrize("target", ["postgres-prod", "host", "kube-apiserver"])
def test_unapproved_targets_are_rejected(target):
    errors = validate_config(cfg(target=target))
    assert errors and "not an approved sandbox resource" in errors[0]


@pytest.mark.parametrize("fault", ["dependency_pause", "dependency_stop", "network_latency", "cpu_exhaustion"])
def test_webserver_only_allows_filling_its_scratch_disk(fault):
    extra = {"network_latency": {"latency_ms": 100}, "cpu_exhaustion": {"cpu_limit": 0.1}}.get(fault, {})
    errors = validate_config(cfg(target="webserver", fault_type=fault, **extra))
    assert errors and "not permitted on 'webserver'" in errors[0]
    assert validate_config(cfg(target="webserver", fault_type="disk_exhaustion")) == []


@pytest.mark.parametrize("target", ["db; rm -rf /", "../db", "DB", "db && docker kill", "$(id)", ""])
def test_target_injection_attempts_fail_structural_validation(target):
    with pytest.raises(ValidationError):
        cfg(target=target)


def test_arbitrary_extra_fields_like_commands_are_rejected():
    with pytest.raises(ValidationError):
        ExperimentConfig(target="db", command="docker rm -f paperless")
    with pytest.raises(ValidationError):
        ExperimentConfig(target="db", thresholds={"max_error_rate": 0.5, "shell": "x"})


def test_unknown_fault_type_rejected():
    with pytest.raises(ValidationError):
        cfg(fault_type="delete_volume")


@pytest.mark.parametrize("duration", [0, 4, config.LIMITS.max_duration_s + 1, 3600])
def test_duration_outside_bounds_rejected(duration):
    if duration < 1:
        with pytest.raises(ValidationError):
            cfg(duration_s=duration)
        return
    assert any("duration" in e for e in validate_config(cfg(duration_s=duration)))


def test_latency_bounds_and_pairing():
    assert any("required" in e for e in validate_config(cfg(fault_type="network_latency")))
    assert any("outside" in e for e in validate_config(cfg(fault_type="network_latency", latency_ms=99999)))
    assert any("only valid" in e for e in validate_config(cfg(latency_ms=100)))


def test_fault_not_permitted_for_target():
    limits = config.SafetyLimits(allowlist={"db": frozenset({config.FAULT_LATENCY})})
    assert any("not permitted" in e for e in validate_config(cfg(), limits))


def test_threshold_bounds_enforced():
    assert any("window" in e for e in validate_config(cfg(thresholds=Thresholds(window_s=1000))))
    assert any("latency threshold" in e for e in validate_config(cfg(thresholds=Thresholds(max_p95_latency_ms=10))))
    with pytest.raises(ValidationError):
        Thresholds(max_error_rate=0)
    with pytest.raises(ValidationError):
        Thresholds(max_error_rate=1.5)


# ---------------------------------------------------------------- thresholds
def s(ts, ok, lat=20.0, probe="api_documents"):
    return {"ts": ts, "ok": ok, "latency_ms": lat, "probe": probe}


def test_error_rate_breach_detected():
    now = 100.0
    rows = [s(95, True), s(96, False, 5000), s(97, False, 5000), s(98, False, 5000)]
    b = evaluate_thresholds(rows, Thresholds(max_error_rate=0.5, max_p95_latency_ms=10000, window_s=10), now)
    assert [x.metric for x in b] == ["error_rate"]
    assert b[0].value == 0.75


def test_latency_breach_detected():
    now = 100.0
    rows = [s(95, True, 3000), s(96, True, 3500), s(97, True, 4000)]
    b = evaluate_thresholds(rows, Thresholds(max_error_rate=0.9, max_p95_latency_ms=1000, window_s=10), now)
    assert [x.metric for x in b] == ["p95_latency_ms"]


def test_no_breach_below_min_samples():
    rows = [s(99, False, 9000), s(99.5, False, 9000)]
    assert evaluate_thresholds(rows, Thresholds(min_samples=3), 100.0) == []


def test_samples_outside_window_are_ignored():
    rows = [s(10, False, 9000)] * 10 + [s(99, True), s(99.2, True), s(99.4, True)]
    assert evaluate_thresholds(rows, Thresholds(window_s=5), 100.0) == []


def test_non_critical_probes_do_not_trigger_abort():
    rows = [s(99, False, 9000, probe="doc_processing")] * 5
    assert evaluate_thresholds(rows, Thresholds(), 100.0) == []


def test_healthy_traffic_does_not_breach():
    rows = [s(90 + i, True, 30) for i in range(10)]
    assert evaluate_thresholds(rows, Thresholds(), 100.0) == []
