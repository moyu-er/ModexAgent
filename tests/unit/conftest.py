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
