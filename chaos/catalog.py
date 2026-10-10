"""Registered experiment capabilities and application workflows.

This is the *only* menu the AI planner may choose from. Capabilities are
derived from the safety allowlist and limits in config.py, so the catalogue
can never offer something the safety policy would reject.

The workflow table states which services each workload probe is *expected*
to need (from the Paperless-ngx architecture) and how much it matters to a
user. Expectations are priors for planning and for spotting surprises in the
analysis; they are never reported as observations.
"""
from __future__ import annotations

from . import config
from .models import FAULT_LABELS

# probe -> label, criticality weight (sums to 1.0), expected service path
WORKFLOWS: dict[str, dict] = {
    "web_root": {
        "label": "Web UI landing page", "criticality": 0.20,
        "expected_path": ["webserver", "db"],
    },
    "api_documents": {
        "label": "Document listing / search API (read path)", "criticality": 0.30,
        "expected_path": ["webserver", "db"],
    },
    "api_status": {
        "label": "System status endpoint (checks DB and Redis)", "criticality": 0.05,
        "expected_path": ["webserver", "db", "broker"],
    },
    "doc_upload": {
        "label": "Document upload (enqueue for consumption)", "criticality": 0.25,
        # db: token auth on every API call (seen in the Day 1 db-pause runs)
        "expected_path": ["webserver", "broker", "db"],
    },
    "doc_processing": {
        "label": "Document consumption end-to-end (worker, parse, store)", "criticality": 0.20,
        "expected_path": ["webserver", "broker", "db"],
    },
}

FAULT_EFFECT = {
    config.FAULT_PAUSE: "Freezes the target container (docker pause). The process hangs and existing "
                        "connections stay open, so callers see hangs/timeouts rather than refused connections.",
    config.FAULT_LATENCY: "Adds a fixed delay to all egress packets of the target container (tc netem), "
                          "slowing every reply the target sends.",
}


def capabilities(limits: config.SafetyLimits = config.LIMITS) -> list[dict]:
    """One entry per fault type, with its permitted targets and parameter bounds."""
    caps = []
    for fault in (config.FAULT_PAUSE, config.FAULT_LATENCY):
        targets = sorted(t for t, allowed in limits.allowlist.items() if fault in allowed)
        if not targets:
            continue
        params = {"duration_s": {"min": limits.min_duration_s, "max": limits.max_duration_s}}
        if fault == config.FAULT_LATENCY:
            params["latency_ms"] = {"min": limits.min_latency_ms, "max": limits.max_latency_ms,
                                    "required": True}
        caps.append({"fault_type": fault, "label": FAULT_LABELS[fault], "effect": FAULT_EFFECT[fault],
                     "targets": targets, "parameters": params})
    return caps


def scenarios(limits: config.SafetyLimits = config.LIMITS) -> list[tuple[str, str]]:
    """Every (fault_type, target) pair the catalogue allows: the coverage denominator."""
    return [(c["fault_type"], t) for c in capabilities(limits) for t in c["targets"]]


def threshold_bounds(limits: config.SafetyLimits = config.LIMITS) -> dict:
    return {
        "abort_max_error_rate": {"min_exclusive": 0.0, "max": limits.max_error_rate_ceiling},
        "abort_max_p95_latency_ms": {"min": limits.min_latency_threshold_ms,
                                     "max": limits.max_latency_threshold_ms},
    }


def workflows_through(service: str) -> list[str]:
    return [w for w, spec in WORKFLOWS.items() if service in spec["expected_path"]]
