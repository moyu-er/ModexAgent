from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from framework.core.agent import AgentContext
from framework.core.session_id import SessionId
from framework.core.tool_manager import ToolResult
from framework.core.types import ToolCall
from framework.interceptor.abc import ToolCallContext
from framework.interceptor.builtin.result_limit import ToolResultLimitInterceptor
from framework.memory.history import MessageHistory
from framework.tools.overflow.handler import ToolResultOverflowHandler


def _make_ctx(history_messages: list[dict[str, Any]] | None = None) -> AgentContext:
    history = MagicMock(spec=MessageHistory)
    history.messages = history_messages or []
    return AgentContext(
        system_prompt="test",
        history=history,
        tool_manager=MagicMock(),
        session=SessionId.from_str("test.agent"),
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
        result = ToolResult(tool_name="read_file", result="short")

        async def next_call() -> ToolResult:
            return result

        ctx = _make_ctx()
        call = _make_call()
        out = await interceptor.around_tool_call(ctx, call, next_call)

        assert out is result
        assert out.overflow_processed is False


class TestLongResultOverflows:
    @pytest.mark.asyncio
    async def test_long_result_overflows(self) -> None:
        handler = AsyncMock(spec=ToolResultOverflowHandler)
        handler.max_chars = 50_000
        handler.store_overflow = AsyncMock(return_value=(
            '<tool_result_overflow tool="read_file" total_chars="100" '
            'total_chunks="2" current_chunk="1">...</tool_result_overflow>',
            MagicMock(),
        ))

        interceptor = ToolResultLimitInterceptor(overflow_handler=handler, max_chars=50)
        long_content = "a" * 100
        result = ToolResult(tool_name="read_file", result=long_content, call_id="tc_1")

        async def next_call() -> ToolResult:
            return result

        ctx = _make_ctx()
        call = _make_call()
        out = await interceptor.around_tool_call(ctx, call, next_call)

        assert out.overflow_processed is True
        assert out.result.startswith("<tool_result_overflow")
        handler.store_overflow.assert_awaited_once()
        handler.schedule_cleanup.assert_called_once()


class TestAlreadyProcessedSkips:
    @pytest.mark.asyncio
    async def test_already_processed_skips(self) -> None:
        handler = AsyncMock(spec=ToolResultOverflowHandler)
        interceptor = ToolResultLimitInterceptor(overflow_handler=handler, max_chars=50)
        long_content = "a" * 100
        result = ToolResult(
            tool_name="read_file",
            result=long_content,
            call_id="tc_1",
            overflow_processed=True,
        )

        async def next_call() -> ToolResult:
            return result

        ctx = _make_ctx()
        call = _make_call()
        out = await interceptor.around_tool_call(ctx, call, next_call)

        assert out is result
        handler.store_overflow.assert_not_awaited()


class TestFallbackTruncationWhenNoHandler:
    @pytest.mark.asyncio
    async def test_fallback_truncation_when_no_handler(self) -> None:
        interceptor = ToolResultLimitInterceptor(overflow_handler=None, max_chars=50)
        long_content = "a" * 100
        result = ToolResult(tool_name="read_file", result=long_content, call_id="tc_1")

        async def next_call() -> ToolResult:
            return result

        ctx = _make_ctx()
        call = _make_call()
        out = await interceptor.around_tool_call(ctx, call, next_call)

        assert out.overflow_processed is False
        assert out.result == "a" * 50 + "\n... (truncated, 100 chars total)"
