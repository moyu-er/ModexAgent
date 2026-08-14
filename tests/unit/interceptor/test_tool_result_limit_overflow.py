from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from modex_agent.core.agent import AgentContext
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.tool_manager import ToolResult
from modex_agent.core.types import ToolCall
from modex_agent.interceptor.abc import ToolCallContext
from modex_agent.interceptor.builtin.result_limit import ToolResultLimitInterceptor
from modex_agent.memory.history import ListMessageHistory
from modex_agent.tools.overflow.cleaner import OverflowCleaner
from modex_agent.tools.overflow.handler import ToolResultOverflowHandler
from modex_agent.tools.overflow.local import LocalFileToolOverflowStore


def _make_ctx() -> AgentContext:
    return AgentContext(
        system_prompt="test",
        history=ListMessageHistory(),
        tool_manager=MagicMock(),
        session=SessionInfo.from_str("test.agent"),
    )


def _make_call(tool_name: str = "read_file", call_id: str | None = "tc_1") -> ToolCallContext:
    return ToolCallContext(
        tool_call=ToolCall(tool_name=tool_name, arguments={}, call_id=call_id),
        tool_name=tool_name,
        arguments={},
        session_id="sess_1",
    )


class TestShortResultPassesThrough:
    @pytest.mark.asyncio
    async def test_short_result_passes_through(self) -> None:
        interceptor = ToolResultLimitInterceptor(overflow_handler=None, max_chars=100)
        result = ToolResult.from_text("read_file", "short")
        next_call = AsyncMock(return_value=result)

        ctx = _make_ctx()
        call = _make_call()
        out = await interceptor.around_tool_call(ctx, call, next_call)

        assert out is result
        assert out.overflow_processed is False


class TestLongResultOverflows:
    @pytest.mark.asyncio
    async def test_long_result_overflows(self, tmp_path: Path) -> None:
        store = LocalFileToolOverflowStore(workspace=tmp_path)
        await store.initialize()
        cleaner = OverflowCleaner(store)
        handler = ToolResultOverflowHandler(store=store, cleaner=cleaner)

        interceptor = ToolResultLimitInterceptor(overflow_handler=handler, max_chars=50)
        long_content = "a" * 100
        result = ToolResult.from_text("read_file", long_content, call_id="tc_1")
        next_call = AsyncMock(return_value=result)

        ctx = _make_ctx()
        call = _make_call()
        try:
            out = await interceptor.around_tool_call(ctx, call, next_call)
            await cleaner.flush()
        finally:
            await cleaner.stop()

        assert out.overflow_processed is True
        assert out.message_content().startswith("a" * 50)
        assert "[Full output (100 chars total) saved to:" in out.message_content()
        assert not out.message_content().startswith("<")
        assert out.content_format is None
        assert out.truncatable_paths is None
        message = out.to_message()
        assert "content_format" not in message
        assert "truncatable_paths" not in message
        full_path = tmp_path / "tool_overflow" / "test.agent" / "tc_1" / "full.txt"
        assert full_path.read_text(encoding="utf-8") == long_content


class TestAlreadyProcessedSkips:
    @pytest.mark.asyncio
    async def test_already_processed_skips(self, tmp_path: Path) -> None:
        store = LocalFileToolOverflowStore(workspace=tmp_path)
        cleaner = OverflowCleaner(store)
        handler = ToolResultOverflowHandler(store=store, cleaner=cleaner)
        interceptor = ToolResultLimitInterceptor(overflow_handler=handler, max_chars=50)
        long_content = "a" * 100
        result = ToolResult.from_text(
            "read_file",
            long_content,
            call_id="tc_1",
            overflow_processed=True,
        )
        next_call = AsyncMock(return_value=result)

        ctx = _make_ctx()
        call = _make_call()
        out = await interceptor.around_tool_call(ctx, call, next_call)

        assert out is result
        assert not (tmp_path / "tool_overflow").exists()


class TestFallbackTruncationWhenNoHandler:
    @pytest.mark.asyncio
    async def test_fallback_truncation_when_no_handler(self) -> None:
        interceptor = ToolResultLimitInterceptor(overflow_handler=None, max_chars=50)
        long_content = "a" * 100
        result = ToolResult.from_text("read_file", long_content, call_id="tc_1")
        next_call = AsyncMock(return_value=result)

        ctx = _make_ctx()
        call = _make_call()
        out = await interceptor.around_tool_call(ctx, call, next_call)

        assert out.overflow_processed is False
        assert out.message_content() == "a" * 50 + "\n... (truncated, 100 chars total)"
