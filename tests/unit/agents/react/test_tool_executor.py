"""Unit tests for ToolExecutor — tool-call execution via interceptor chain.

The timeout mechanism is now enforced by ``ToolTimeoutInterceptor`` (composed
as the innermost mandatory interceptor). These tests exercise the executor's
interceptor composition and the timeout interceptor's behaviour.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import pytest

from modex_agent.agents.react.tool_executor import ToolExecutor
from modex_agent.agents.react.state import ReActTurnState
from modex_agent.core.agent import AgentContext
from modex_agent.core.llm_struct import RuntimeSafetyPolicy, TurnTimeoutPolicy
from modex_agent.core.message import ContentFormat
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.tool_manager import ToolResult
from modex_agent.core.types import ToolCall
from modex_agent.interceptor.abc import ToolCallContext
from modex_agent.runtime.enums import AgentKind, TurnPhase
from modex_agent.runtime.models import TurnIdentity
from modex_agent.runtime.services import AgentRuntime, AgentRuntimeServices
from modex_agent.memory.history import ListMessageHistory
from modex_agent.core.tool_manager import InMemoryToolManager

if TYPE_CHECKING:
    from modex_agent.core.capabilities import ModelCapabilities


def _make_ctx(*, tool_manager=None, **kw) -> AgentContext:
    state = ReActTurnState(
        identity=TurnIdentity(
            agent_id="test", session=SessionInfo.from_str("s1"), turn_id="t1"
        ),
        agent_kind=AgentKind.REACT,
        phase=TurnPhase.CREATED,
    )
    runtime = AgentRuntime(services=AgentRuntimeServices(), state=state)
    return AgentContext(
        system_prompt="",
        history=ListMessageHistory(),
        tool_manager=tool_manager or InMemoryToolManager(),
        session=SessionInfo.from_str("test.agent"),
        max_iterations=5,
        identity=state.identity,
        runtime=runtime,
        **kw,
    )


class _RecordingToolManager:
    def __init__(self, tool_coro) -> None:
        self._tool_coro = tool_coro

    async def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        ctx: Any = None,
    ) -> ToolResult:
        return await self._tool_coro(tool_name, arguments)

    def get_tool_descriptions(self, caps: ModelCapabilities | None = None) -> list[str]:
        return []


class _RecordingInterceptorChain:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ToolCallContext]] = []

    async def around_tool_call(self, ctx, call_ctx, next_call) -> ToolResult:
        self.calls.append((ctx, call_ctx))
        return await next_call()


class TestToolExecutorInterceptorWrap:
    @pytest.mark.asyncio
    async def test_interceptor_present_wraps_and_runs_tool(self):
        async def real_tool(tool_name, arguments):
            return ToolResult.from_text(tool_name, "ran")

        ctx = _make_ctx(tool_manager=_RecordingToolManager(real_tool))
        chain = _RecordingInterceptorChain()
        ctx.runtime.services.interceptors = chain  # type: ignore[assignment]

        executor = ToolExecutor()
        tc = ToolCall(tool_name="echo", arguments={"a": 1}, call_id="c1")

        result = await executor.execute(tc, ctx)

        assert len(chain.calls) == 1
        wrapped_ctx, call_ctx = chain.calls[0]
        assert wrapped_ctx is ctx
        assert call_ctx.tool_name == "echo"
        assert result.message_content() == "ran"
        assert result.error is None


class TestToolExecutorRawExecution:
    @pytest.mark.asyncio
    async def test_no_interceptors_runs_with_timeout(self):
        async def real_tool(tool_name, arguments):
            return ToolResult.from_text(tool_name, "ok")

        ctx = _make_ctx(tool_manager=_RecordingToolManager(real_tool))

        executor = ToolExecutor()
        tc = ToolCall(tool_name="echo", arguments={}, call_id="c1")

        result = await executor.execute(tc, ctx)
        assert result.message_content() == "ok"
        assert result.error is None


class TestToolExecutorTimeout:
    @pytest.mark.asyncio
    async def test_slow_tool_returns_timeout_xml_result(self):
        async def slow_tool(tool_name, arguments):
            await asyncio.sleep(0.5)
            return ToolResult.from_text(tool_name, "done")

        ctx = _make_ctx(tool_manager=_RecordingToolManager(slow_tool))
        ctx.runtime.services.safety = RuntimeSafetyPolicy(
            turn=TurnTimeoutPolicy(tool_timeout_seconds=0.01),
        )

        executor = ToolExecutor()
        tc = ToolCall(tool_name="slow_tool", arguments={}, call_id="c1")

        result = await executor.execute(tc, ctx)

        assert result.error is not None
        assert "timed out" in result.error.lower()
        assert result.message_content()
        assert "<tool_timeout>" in result.message_content()
        assert result.content_format == ContentFormat.XML

    @pytest.mark.asyncio
    async def test_safety_present_overrides_default(self):
        async def slow_tool(tool_name, arguments):
            await asyncio.sleep(0.5)
            return ToolResult.from_text(tool_name, "done")

        ctx = _make_ctx(tool_manager=_RecordingToolManager(slow_tool))
        ctx.runtime.services.safety = RuntimeSafetyPolicy(
            turn=TurnTimeoutPolicy(tool_timeout_seconds=0.01),
        )

        executor = ToolExecutor()
        tc = ToolCall(tool_name="slow_tool", arguments={}, call_id="c1")

        result = await executor.execute(tc, ctx)
        assert result.error is not None
        assert "timed out" in result.error.lower()

    @pytest.mark.asyncio
    async def test_fast_tool_completes_under_default_timeout(self):
        async def fast_tool(tool_name, arguments):
            return ToolResult.from_text(tool_name, "ok")

        ctx = _make_ctx(tool_manager=_RecordingToolManager(fast_tool))

        executor = ToolExecutor()
        tc = ToolCall(tool_name="fast_tool", arguments={}, call_id="c1")

        result = await executor.execute(tc, ctx)
        assert result.message_content() == "ok"
        assert result.error is None

    @pytest.mark.asyncio
    async def test_timeout_does_not_terminate_turn(self):
        async def slow_then_fast(tool_name, arguments):
            if tool_name == "slow":
                await asyncio.sleep(0.3)
                return ToolResult.from_text(tool_name, "slow_done")
            return ToolResult.from_text(tool_name, "fast_done")

        ctx = _make_ctx(tool_manager=_RecordingToolManager(slow_then_fast))
        ctx.runtime.services.safety = RuntimeSafetyPolicy(
            turn=TurnTimeoutPolicy(tool_timeout_seconds=0.05),
        )

        executor = ToolExecutor()

        tc1 = ToolCall(tool_name="slow", arguments={}, call_id="c1")
        result1 = await executor.execute(tc1, ctx)
        assert result1.error is not None
        assert "<tool_timeout>" in result1.message_content()

        tc2 = ToolCall(tool_name="fast", arguments={}, call_id="c2")
        result2 = await executor.execute(tc2, ctx)
        assert result2.message_content() == "fast_done"
        assert result2.error is None
