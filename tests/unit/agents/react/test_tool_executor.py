"""Unit tests for ToolExecutor — extracted tool-call execution collaborator.

These mirror the behaviour previously covered only end-to-end via
``ReActAgent.run()`` (see ``tests/unit/agents/test_react_agent_error.py::
TestReActAgentToolTimeout``). Here we exercise the collaborator directly so the
branch-for-branch port from ``ReActAgent._execute_tool`` /
``_execute_tool_raw`` / ``_resolve_tool_timeout`` is pinned independently of
the agent / graph wiring (cutover is a later, atomic task).
"""

import asyncio
from typing import Any

import pytest

from modex_agent.agents.react.tool_executor import ToolExecutor
from modex_agent.agents.react.state import ReActTurnState
from modex_agent.core.agent import AgentContext
from modex_agent.core.llm_struct import RuntimeSafetyPolicy, TurnTimeoutPolicy
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.tool_manager import ToolResult
from modex_agent.core.types import ToolCall
from modex_agent.interceptor.abc import ToolCallContext
from modex_agent.runtime.enums import AgentKind, TurnPhase
from modex_agent.runtime.models import TurnIdentity
from modex_agent.runtime.services import AgentRuntime, AgentRuntimeServices
from modex_agent.memory.history import ListMessageHistory
from modex_agent.core.tool_manager import InMemoryToolManager


def _make_ctx(*, tool_manager=None, **kw) -> AgentContext:
    """Create a real AgentContext with typed runtime state."""
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
    """Minimal tool_manager fake: dispatches to a registered async callable."""

    def __init__(self, tool_coro) -> None:
        self._tool_coro = tool_coro

    async def execute(self, tool_name: str, arguments: dict[str, Any]) -> ToolResult:
        return await self._tool_coro(tool_name, arguments)

    def get_tool_descriptions(self) -> list[str]:
        return []


class _RecordingInterceptorChain:
    """Fake InterceptorChain: records that around_tool_call was invoked once,
    then runs the inner ``next_call`` unchanged so the real tool still executes."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ToolCallContext]] = []

    async def around_tool_call(self, ctx, call_ctx, next_call) -> ToolResult:
        self.calls.append((ctx, call_ctx))
        return await next_call()


class TestToolExecutorInterceptorWrap:
    @pytest.mark.asyncio
    async def test_interceptor_present_wraps_and_runs_tool(self):
        """When ctx.runtime.interceptors is set, around_tool_call is invoked
        exactly once AND the underlying tool still runs and returns its result."""

        async def real_tool(tool_name, arguments):
            return ToolResult(tool_name=tool_name, result="ran")

        ctx = _make_ctx(tool_manager=_RecordingToolManager(real_tool))
        chain = _RecordingInterceptorChain()
        ctx.runtime.services.interceptors = chain  # type: ignore[assignment]

        executor = ToolExecutor(default_tool_timeout=5.0)
        tc = ToolCall(tool_name="echo", arguments={"a": 1}, call_id="c1")

        result = await executor.execute(tc, ctx)

        assert len(chain.calls) == 1, "interceptor chain must be invoked exactly once"
        wrapped_ctx, call_ctx = chain.calls[0]
        assert wrapped_ctx is ctx
        assert call_ctx.tool_name == "echo"
        assert call_ctx.arguments == {"a": 1}
        assert call_ctx.session_id == str(ctx.session)
        assert result.result == "ran"
        assert result.error is None


class TestToolExecutorRawExecution:
    @pytest.mark.asyncio
    async def test_no_interceptors_runs_raw(self):
        """Without interceptors, execute falls through to raw execution and
        returns the tool's ToolResult."""

        async def real_tool(tool_name, arguments):
            return ToolResult(tool_name=tool_name, result="ok")

        ctx = _make_ctx(tool_manager=_RecordingToolManager(real_tool))
        # No interceptors configured on services.

        executor = ToolExecutor(default_tool_timeout=5.0)
        tc = ToolCall(tool_name="echo", arguments={}, call_id="c1")

        result = await executor.execute(tc, ctx)
        assert result.result == "ok"
        assert result.error is None


class TestToolExecutorTimeout:
    @pytest.mark.asyncio
    async def test_slow_tool_returns_timeout_error_result(self):
        """A tool exceeding default_tool_timeout returns a ToolResult whose
        ``error`` mentions "timeout". Must NOT raise.

        Note: AgentRuntimeServices always carries a default RuntimeSafetyPolicy,
        so ``_resolve_tool_timeout`` only reaches the ctor-default branch when
        ``ctx.runtime is None``. We null runtime here (tool_manager lives on
        AgentContext directly, so execution still works) to exercise that path —
        this mirrors how the original ReActAgent._resolve_tool_timeout behaves.
        """

        async def slow_tool(tool_name, arguments):
            await asyncio.sleep(0.5)
            return ToolResult(tool_name=tool_name, result="done")

        ctx = _make_ctx(tool_manager=_RecordingToolManager(slow_tool))
        ctx.runtime = None
        executor = ToolExecutor(default_tool_timeout=0.01)
        tc = ToolCall(tool_name="slow_tool", arguments={}, call_id="c1")

        result = await executor.execute(tc, ctx)

        assert result.result is None
        assert result.error is not None
        assert "timeout" in result.error.lower()


class TestToolExecutorResolveTimeout:
    @pytest.mark.asyncio
    async def test_safety_present_overrides_default(self):
        """When runtime.safety is present, safety.turn.tool_timeout_seconds
        is used (here a near-zero timeout forces the slow tool to time out)."""

        async def slow_tool(tool_name, arguments):
            await asyncio.sleep(0.5)
            return ToolResult(tool_name=tool_name, result="done")

        ctx = _make_ctx(tool_manager=_RecordingToolManager(slow_tool))
        ctx.runtime.services.safety = RuntimeSafetyPolicy(
            turn=TurnTimeoutPolicy(tool_timeout_seconds=0.01),
        )

        # default_tool_timeout is deliberately large; safety must win.
        executor = ToolExecutor(default_tool_timeout=10.0)
        tc = ToolCall(tool_name="slow_tool", arguments={}, call_id="c1")

        result = await executor.execute(tc, ctx)
        assert result.error is not None
        assert "timeout" in result.error.lower()

    @pytest.mark.asyncio
    async def test_safety_absent_falls_back_to_default(self):
        """When ctx.runtime is None, _resolve_tool_timeout falls back to the
        ctor default_tool_timeout. With a generous default and a fast tool the
        call completes normally, proving the fallback branch is reachable."""

        async def fast_tool(tool_name, arguments):
            return ToolResult(tool_name=tool_name, result="ok")

        ctx = _make_ctx(tool_manager=_RecordingToolManager(fast_tool))
        ctx.runtime = None

        executor = ToolExecutor(default_tool_timeout=5.0)
        tc = ToolCall(tool_name="fast_tool", arguments={}, call_id="c1")

        result = await executor.execute(tc, ctx)
        assert result.result == "ok"
        assert result.error is None
