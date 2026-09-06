from __future__ import annotations

import os
from collections.abc import Generator
from typing import Any, Final

import pytest

from modex_agent.agents.react.state import ReActTurnState
from modex_agent.runtime.enums import TurnCustomKey

_MAX_PARALLEL_ENV: Final = "PYTEST_MAX_PARALLEL_TOOL_CALLS"


@pytest.fixture(autouse=True)
def _force_max_parallel_tool_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[None]:
    configured = os.environ.get(_MAX_PARALLEL_ENV)
    if configured is None:
        yield
        return

    max_parallel = int(configured)
    original_init = ReActTurnState.__init__

    def init_with_max_parallel(self: ReActTurnState, **data: Any) -> None:
        original_init(self, **data)
        self.custom[TurnCustomKey.MAX_PARALLEL_TOOL_CALLS] = max_parallel

    monkeypatch.setattr(ReActTurnState, "__init__", init_with_max_parallel)
    yield


@pytest.fixture(autouse=True)
def _fake_modexctl_bin(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """Hermetic modexctl resolution for unit tests.

    Native-agent assembly derives the native_env hook's env spec eagerly,
    which resolves the modexctl bin dir — on machines without modexctl
    installed that resolution raises. Point it at a fake binary so the
    unit suite stays hermetic. Resolver-specific tests clear the variable
    explicitly (``patch.dict(..., clear=True)``). The fake lives OUTSIDE
    the test's own ``tmp_path`` — several tests assert on their tmp dir's
    emptiness.
    """
    bin_dir = tmp_path_factory.mktemp("modexctl-bin")
    (bin_dir / "modexctl.bat").write_text("@exit /b 0\n", encoding="ascii")
    monkeypatch.setenv("MODEXBOT_BIN_DIR", str(bin_dir))
