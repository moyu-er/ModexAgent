"""Unit tests for ToolTimeoutInterceptor."""

from __future__ import annotations

import asyncio

import pytest

from modex_agent.agents.react.state import ReActTurnState
from modex_agent.core.agent import AgentContext
from modex_agent.core.llm_struct import RuntimeSafetyPolicy, TurnTimeoutPolicy
from modex_agent.core.message import ContentFormat
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.tool_manager import InMemoryToolManager, ToolResult
from modex_agent.core.types import ToolCall
from modex_agent.interceptor.abc import ToolCallContext
from modex_agent.interceptor.builtin.tool_timeout import ToolTimeoutInterceptor
from modex_agent.memory.history import ListMessageHistory
from modex_agent.runtime.enums import AgentKind, TurnPhase
from modex_agent.runtime.models import TurnIdentity
from modex_agent.runtime.services import AgentRuntime, AgentRuntimeServices


def _make_ctx(*, safety=None) -> AgentContext:
    state = ReActTurnState(
        identity=TurnIdentity(
            agent_id="test", session=SessionInfo.from_str("s1"), turn_id="t1"
        ),
        agent_kind=AgentKind.REACT,
        phase=TurnPhase.CREATED,
    )
    services = AgentRuntimeServices()
    if safety is not None:
        services.safety = safety
    runtime = AgentRuntime(services=services, state=state)
    return AgentContext(
        system_prompt="",
        history=ListMessageHistory(),
        tool_manager=InMemoryToolManager(),
        session=SessionInfo.from_str("test.agent"),
        max_iterations=5,
        identity=state.identity,
        runtime=runtime,
    )


def _call_ctx(name: str = "test_tool") -> ToolCallContext:
    return ToolCallContext(
        tool_call=ToolCall(tool_name=name, arguments={}, call_id="c1"),
        tool_name=name,
        arguments={},
        session_id="s1",
    )


class TestToolTimeoutInterceptorTimeout:
    @pytest.mark.asyncio
    async def test_timeout_returns_xml_result(self):
        async def slow_tool():
            await asyncio.sleep(0.5)
            return ToolResult.from_text("slow", "done")

        safety = RuntimeSafetyPolicy(
            turn=TurnTimeoutPolicy(tool_timeout_seconds=0.01),
        )
        ctx = _make_ctx(safety=safety)
        interceptor = ToolTimeoutInterceptor()

        result = await interceptor.around_tool_call(ctx, _call_ctx(), slow_tool)

        assert result.error is not None
        assert "timed out" in result.error.lower()
        assert result.message_content()
        assert "<tool_timeout>" in result.message_content()
        assert "</tool_timeout>" in result.message_content()
        assert "timed_out" in result.message_content()
        assert result.content_format == ContentFormat.XML
        assert result.truncatable_paths == []

    @pytest.mark.asyncio
    async def test_timeout_result_is_failed(self):
        async def slow_tool():
            await asyncio.sleep(0.5)
            return ToolResult.from_text("slow", "done")

        safety = RuntimeSafetyPolicy(
            turn=TurnTimeoutPolicy(tool_timeout_seconds=0.01),
        )
        ctx = _make_ctx(safety=safety)
        interceptor = ToolTimeoutInterceptor()

        result = await interceptor.around_tool_call(ctx, _call_ctx(), slow_tool)

        assert not result.success

    @pytest.mark.asyncio
    async def test_fast_tool_completes(self):
        async def fast_tool():
            return ToolResult.from_text("fast", "ok")

        safety = RuntimeSafetyPolicy(
            turn=TurnTimeoutPolicy(tool_timeout_seconds=10.0),
        )
        ctx = _make_ctx(safety=safety)
        interceptor = ToolTimeoutInterceptor()

        result = await interceptor.around_tool_call(ctx, _call_ctx(), fast_tool)

        assert result.message_content() == "ok"
        assert result.error is None


class TestToolTimeoutInterceptorCancellation:
    @pytest.mark.asyncio
    async def test_cancelled_error_propagates(self):
        async def cancelled_tool():
            raise asyncio.CancelledError()

        safety = RuntimeSafetyPolicy(
            turn=TurnTimeoutPolicy(tool_timeout_seconds=10.0),
        )
        ctx = _make_ctx(safety=safety)
        interceptor = ToolTimeoutInterceptor()

        with pytest.raises(asyncio.CancelledError):
            await interceptor.around_tool_call(ctx, _call_ctx(), cancelled_tool)

    @pytest.mark.asyncio
    async def test_external_task_cancel_propagates(self):
        async def slow_tool():
            await asyncio.sleep(10.0)
            return ToolResult.from_text("slow", "done")

        safety = RuntimeSafetyPolicy(
            turn=TurnTimeoutPolicy(tool_timeout_seconds=30.0),
        )
        ctx = _make_ctx(safety=safety)
        interceptor = ToolTimeoutInterceptor()

        task = asyncio.create_task(
            interceptor.around_tool_call(ctx, _call_ctx(), slow_tool)
        )
        await asyncio.sleep(0.05)
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task


class TestToolTimeoutInterceptorResolution:
    @pytest.mark.asyncio
    async def test_safety_present_used(self):
        async def slow_tool():
            await asyncio.sleep(0.5)
            return ToolResult.from_text("slow", "done")

        safety = RuntimeSafetyPolicy(
            turn=TurnTimeoutPolicy(tool_timeout_seconds=0.01),
        )
        ctx = _make_ctx(safety=safety)
        interceptor = ToolTimeoutInterceptor()

        result = await interceptor.around_tool_call(ctx, _call_ctx(), slow_tool)

        assert result.error is not None
        assert "0" in result.error

    @pytest.mark.asyncio
    async def test_no_runtime_falls_back_to_default(self):
        async def fast_tool():
            return ToolResult.from_text("fast", "ok")

        ctx = _make_ctx()
        ctx.runtime = None
        interceptor = ToolTimeoutInterceptor()

        result = await interceptor.around_tool_call(ctx, _call_ctx(), fast_tool)

        assert result.message_content() == "ok"
        assert result.error is None


class TestToolTimeoutInterceptorWatchdogFloor:
    """Phase-budget protocol: the interceptor declares its full budget into
    the dispatch deadline at entry so the outer watchdog never fires first."""

    @pytest.mark.asyncio
    async def test_entry_floor_raises_remaining_to_budget_plus_margin(self):
        from modex_agent.runtime.dispatch import (
            DispatchDeadline,
            current_dispatch_deadline,
        )

        safety = RuntimeSafetyPolicy(
            turn=TurnTimeoutPolicy(tool_timeout_seconds=100.0),
        )
        ctx = _make_ctx(safety=safety)

        # Near-expiry deadline (simulating a long turn tail): 0.05s left.
        deadline = DispatchDeadline(initial_timeout=0.05, max_ahead_seconds=1200.0)
        token = current_dispatch_deadline.set(deadline)
        try:
            captured: dict[str, float] = {}

            async def probe_tool():
                captured["remaining_at_tool"] = deadline.remaining
                return ToolResult.from_text("probe", "ok")

            interceptor = ToolTimeoutInterceptor()
            await interceptor.around_tool_call(ctx, _call_ctx(), probe_tool)

            margin = safety.deadline.phase_margin_seconds
            assert captured["remaining_at_tool"] >= 100.0 + margin - 1.0
        finally:
            current_dispatch_deadline.reset(token)

    @pytest.mark.asyncio
    async def test_entry_floor_noop_without_deadline(self):
        # No deadline on the ContextVar (clean mode / dispatch_timeout=0):
        # the floor renewal must be a silent no-op.
        async def fast_tool():
            return ToolResult.from_text("fast", "ok")

        safety = RuntimeSafetyPolicy(
            turn=TurnTimeoutPolicy(tool_timeout_seconds=10.0),
        )
        ctx = _make_ctx(safety=safety)
        interceptor = ToolTimeoutInterceptor()

        result = await interceptor.around_tool_call(ctx, _call_ctx(), fast_tool)
        assert result.message_content() == "ok"
