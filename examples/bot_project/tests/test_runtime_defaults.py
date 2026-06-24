"""Tests for bot runtime defaults."""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from bot.service.core import BotService

from modex_agent.core.session_id import SessionInfo
from modex_agent.core.types import InputMessage
from modex_agent.interceptor.builtin import (
    ToolResultLimitInterceptor,
)
from modex_agent.pipeline.adapters import InputAdapter, NullOutputAdapter


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
            yield InputMessage(content="", session=SessionInfo.from_str("s1", default_agent_name="main"))


def test_default_interceptor_chain_keeps_only_effective_defaults() -> None:
    """The per-workspace interceptor chain (re-homed from BotService into
    wiring) still installs the ToolResultLimitInterceptor."""
    from unittest.mock import MagicMock

    from bot.workspace.wiring import _build_workspace_interceptor_chain
    from modex_agent.tools.overflow.local import LocalFileToolOverflowStore

    service = MagicMock()
    service.control_channel = MagicMock()
    overflow_store = LocalFileToolOverflowStore(
        workspace=Path("/tmp/_test_overflow"), max_chunk_size=10_000
    )
    chain = _build_workspace_interceptor_chain(service, overflow_store)
    interceptors = chain.interceptors

    assert any(isinstance(item, ToolResultLimitInterceptor) for item in interceptors)


def test_tool_timeout_exceeds_shell_internal_timeout() -> None:
    """Outer tool timeout must strictly exceed CommandTool.timeout so the shell
    can return structured timeout XML with partial output instead of being
    cancelled by the ReAct-level asyncio.wait_for."""
    from modex_agent.tools.terminal import SubprocessTool

    service = BotService(
        config_dir=Path("examples/bot_project/config"),
        input_adapter=_InputAdapter(),
        output_adapter=NullOutputAdapter(),
        emitter_factory=lambda _session_id: None,
    )
    safety = service.safety_policy
    shell_timeout = SubprocessTool().timeout
    assert safety.turn.tool_timeout_seconds > shell_timeout, (
        f"tool_timeout_seconds ({safety.turn.tool_timeout_seconds}) must be > "
        f"SubprocessTool.timeout ({shell_timeout})"
    )
