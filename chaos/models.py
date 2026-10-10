"""Validated experiment configuration and shared data types."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from .config import FAULT_LATENCY, FAULT_PAUSE


class Thresholds(BaseModel):
    """Automatic abort thresholds evaluated over a rolling window while the
    fault is active."""

    model_config = ConfigDict(extra="forbid")

    max_error_rate: float = Field(0.5, gt=0.0, le=1.0)
    max_p95_latency_ms: int = Field(4000, ge=1)
    window_s: int = Field(10, ge=1)
    min_samples: int = Field(3, ge=1, le=100)


class ExperimentConfig(BaseModel):
    """What the user may ask for. Unknown fields are rejected, so there is no
    way to smuggle commands, images or container names through the API."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field("experiment", min_length=1, max_length=80)
    hypothesis: str = Field("", max_length=400)
    fault_type: Literal["dependency_pause", "network_latency"] = FAULT_PAUSE
    target: str = Field(..., pattern=r"^[a-z0-9][a-z0-9_-]{0,40}$")
    duration_s: int = Field(30, ge=1)
    latency_ms: Optional[int] = Field(None, ge=1)
    thresholds: Thresholds = Field(default_factory=Thresholds)
    recovery_timeout_s: int = Field(90, ge=5)


class RunState(str, Enum):
    PREFLIGHT = "preflight"
    BASELINE = "baseline"
    INJECTING = "injecting"
    ACTIVE = "active"
    ROLLING_BACK = "rolling_back"
    VERIFYING_RECOVERY = "verifying_recovery"
    OBSERVING = "observing"
    COMPLETED = "completed"
    ABORTED = "aborted"
    FAILED = "failed"
    REJECTED = "rejected"
    INTERRUPTED = "interrupted"


TERMINAL_STATES = {
    RunState.COMPLETED,
    RunState.ABORTED,
    RunState.FAILED,
    RunState.REJECTED,
    RunState.INTERRUPTED,
}


@dataclass
class ProbeResult:
    probe: str
    ok: bool
    latency_ms: float
    status: Optional[int] = None
    detail: str = ""
    ts: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


FAULT_LABELS = {
    FAULT_PAUSE: "Dependency pause (docker pause / unpause)",
    FAULT_LATENCY: "Network latency (tc netem on target egress)",
}
