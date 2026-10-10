import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chaos import config  # noqa: E402
from chaos.engine import Engine  # noqa: E402
from chaos.storage import Store  # noqa: E402
from tests.fakes import FakeClient, FakeDocker  # noqa: E402

FAST_TIMINGS = config.Timings(pre_baseline_s=0.6, post_observe_s=0.3, monitor_tick_s=0.1,
                              recovery_success_streak=2, removal_retries=2)
TEST_LIMITS = config.SafetyLimits(min_duration_s=1, min_window_s=1)
FAST_WORKLOAD = {"intervals": {"web_root": 0.1, "api_documents": 0.1, "api_status": 0.1, "doc_upload": 0.2},
                 "health_interval": 0.2}


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "test.sqlite3")


@pytest.fixture
def docker():
    return FakeDocker()


@pytest.fixture
def engine(store, docker):
    return Engine(store, docker, lambda: FakeClient(docker), timings=FAST_TIMINGS, limits=TEST_LIMITS,
                  workload_kwargs=FAST_WORKLOAD, spawn_watchdog=False)
