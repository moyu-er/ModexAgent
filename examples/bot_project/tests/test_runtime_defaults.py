"""Tests for bot runtime defaults."""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


from modex_agent.core.session_id import SessionInfo
from modex_agent.core.types import InputMessage
from modex_agent.interceptor.builtin import (
    ToolResultLimitInterceptor,
)
from modex_agent.pipeline.adapters import InputAdapter


class _InputAdapter(InputAdapter):
    @property
    def name(self) -> str:
        return "test"

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def receive(self) -> AsyncIterator[InputMessage]:
        if False:
            yield InputMessage(content="", session=SessionInfo.from_str("s1"))


def test_default_interceptor_chain_keeps_only_effective_defaults() -> None:
    """The per-workspace interceptor chain (re-homed from BotService into
    wiring) still installs the ToolResultLimitInterceptor."""
    from bot.workspace.wiring import build_tool_overflow_interceptor_chain

    from modex_agent.tools.overflow.local import LocalFileToolOverflowStore

    overflow_store = LocalFileToolOverflowStore(
        workspace=Path("/tmp/_test_overflow")
    )
    chain = build_tool_overflow_interceptor_chain(overflow_store)
    interceptors = chain.interceptors

    assert any(isinstance(item, ToolResultLimitInterceptor) for item in interceptors)


def test_tool_timeout_default_is_540() -> None:
    """Framework default tool timeout is 540 seconds, enforced by
    ToolTimeoutInterceptor (persistent bash ladder: 480s tool < 540s interceptor)."""
    from modex_agent.core.constants import DefaultValues

    assert DefaultValues.TOOL_TIMEOUT_SECONDS == 540.0
