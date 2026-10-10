"""Static configuration: paths, sandbox scope and hard safety limits.

Everything an experiment may touch is declared here. The API and dashboard
can only *select* from these values; they cannot extend them at runtime.
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SANDBOX_DIR = ROOT / "sandbox"
COMPOSE_FILE = SANDBOX_DIR / "docker-compose.yml"
SANDBOX_ENV_FILE = SANDBOX_DIR / ".env"
DATA_DIR = Path(os.environ.get("CHAOS_DATA_DIR", ROOT / "data"))
DB_PATH = DATA_DIR / "chaos.sqlite3"
GRAPH_OVERRIDES_FILE = DATA_DIR / "graph_overrides.json"
DASHBOARD_DIR = ROOT / "dashboard"

COMPOSE_PROJECT = "paperless-chaos"
SANDBOX_LABEL = "chaos.sandbox"
NETEM_IMAGE = "chaos-netem:local"
NETEM_LABEL = "chaos.netem"

PAPERLESS_URL = os.environ.get("CHAOS_PAPERLESS_URL", "http://127.0.0.1:8000")
CONTROLLER_PORT = int(os.environ.get("CHAOS_PORT", "8800"))


def docker_bin() -> str:
    found = os.environ.get("CHAOS_DOCKER_BIN") or shutil.which("docker")
    if found:
        return found
    default = Path(r"C:\Program Files\Docker\Docker\resources\bin\docker.exe")
    return str(default) if default.exists() else "docker"


# Fault types the controller knows how to inject and remove.
FAULT_PAUSE = "dependency_pause"
FAULT_LATENCY = "network_latency"


@dataclass(frozen=True)
class SafetyLimits:
    """Hard limits. Requests outside them are rejected, never clamped."""

    # service name -> fault types permitted against it. The webserver is the
    # system under observation and is deliberately not a permitted target.
    allowlist: dict[str, frozenset[str]] = field(
        default_factory=lambda: {
            "db": frozenset({FAULT_PAUSE, FAULT_LATENCY}),
            "broker": frozenset({FAULT_PAUSE, FAULT_LATENCY}),
        }
    )
    min_duration_s: int = 5
    max_duration_s: int = 120
    min_latency_ms: int = 10
    max_latency_ms: int = 3000
    # bounds for user-configurable abort thresholds
    max_error_rate_ceiling: float = 1.0
    min_latency_threshold_ms: int = 100
    max_latency_threshold_ms: int = 30000
    min_window_s: int = 3
    max_window_s: int = 60
    # watchdog removes the fault this long after the planned end, even if the
    # controller process is dead
    watchdog_grace_s: int = 10
    max_recovery_timeout_s: int = 300


LIMITS = SafetyLimits()


@dataclass(frozen=True)
class Timings:
    """Phase timings. Tests shrink these; production uses the defaults."""

    pre_baseline_s: float = 15.0
    post_observe_s: float = 10.0
    monitor_tick_s: float = 1.0
    recovery_success_streak: int = 3
    removal_retries: int = 3


TIMINGS = Timings()
