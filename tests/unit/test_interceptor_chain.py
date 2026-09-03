"""Tests for InterceptorChain — AOP onion chain execution."""

from __future__ import annotations

import pytest

from modex_agent.control.exceptions import AgentCancelledError, AgentControlError, AgentTimeoutError
from modex_agent.core.agent import AgentContext
from modex_agent.core.emitter import AgentResult
from modex_agent.core.message import ToolCall
from modex_agent.core.tool_manager import ToolResult
from modex_agent.interceptor.abc import (
    IterationContext,
    IterationInterceptor,
    ToolCallContext,
    ToolCallInterceptor,
    ToolCallNext,
    TurnInterceptor,
    TurnNext,
)
from modex_agent.interceptor.chain import InterceptorChain


class BoomInterceptor(ToolCallInterceptor):
    """Interceptor that raises a plain exception."""

    @property
    def name(self) -> str:
        return "boom"

    async def around_tool_call(
        self,
        ctx: AgentContext,
        call: ToolCallContext,
        next_call: ToolCallNext,
    ) -> ToolResult:
        raise RuntimeError("boom")


class ControlErrorInterceptor(ToolCallInterceptor):
    """Interceptor that raises an AgentControlError subclass."""

    @property
    def name(self) -> str:
        return "control-error"

    def __init__(self, exc: AgentControlError) -> None:
        self._exc = exc

    async def around_tool_call(
        self,
        ctx: AgentContext,
        call: ToolCallContext,
        next_call: ToolCallNext,
    ) -> ToolResult:
        raise self._exc


class OrderInterceptor(ToolCallInterceptor):
    """Records enter/exit order to verify onion wrapping."""

    def __init__(self, name: str, log: list[str]) -> None:
        self._log_name = name
        self._log = log

    @property
    def name(self) -> str:
        return self._log_name

    async def around_tool_call(
        self,
        ctx: AgentContext,
        call: ToolCallContext,
        next_call: ToolCallNext,
    ) -> ToolResult:
        self._log.append(f"{self._log_name}_in")
        result = await next_call()
        self._log.append(f"{self._log_name}_out")
        return result


class ShortCircuitInterceptor(ToolCallInterceptor):
    """Returns a substitute result without calling next_call."""

    @property
    def name(self) -> str:
        return "short-circuit"

    def __init__(self, result: ToolResult) -> None:
        self._result = result

    async def around_tool_call(
        self,
        ctx: AgentContext,
        call: ToolCallContext,
        next_call: ToolCallNext,
    ) -> ToolResult:
        return self._result


class FakeCtx:
    """Minimal fake AgentContext for testing."""

    def __init__(self) -> None:
        self.session_id = "test-session"
        self.metadata = {}


def _make_tool_call_ctx(tool_name: str = "test_tool") -> ToolCallContext:
    return ToolCallContext(
        tool_call=ToolCall(tool_name=tool_name, arguments={}, call_id="call-1"),
        tool_name=tool_name,
        arguments={},
        session_id="test-session",
        turn_id="turn-1",
    )


@pytest.fixture
def fake_ctx():
    return FakeCtx()


class TestInterceptorChainToolFallback:
    """InterceptorChain.around_tool_call must return a valid ToolResult
    even when an interceptor raises a plain exception.
    """

    @pytest.mark.asyncio
    async def test_plain_exception_converted_to_tool_result(self, fake_ctx):
        chain = InterceptorChain([BoomInterceptor()])
        call_ctx = _make_tool_call_ctx()

        async def actual() -> ToolResult:
            return ToolResult.from_text("test_tool", "ok")

        result = await chain.around_tool_call(fake_ctx, call_ctx, actual)

        assert isinstance(result, ToolResult)
        assert result.tool_name == "test_tool"
        assert result.error is not None
        assert "boom" in result.error

    @pytest.mark.asyncio
    async def test_cancelled_error_propagates(self, fake_ctx):
        """asyncio.CancelledError must propagate, not be swallowed."""
        exc = AgentCancelledError("user cancelled")
        chain = InterceptorChain([ControlErrorInterceptor(exc)])
        call_ctx = _make_tool_call_ctx()

        async def actual() -> ToolResult:
            return ToolResult.from_text("test_tool", "ok")

        with pytest.raises(AgentCancelledError):
            await chain.around_tool_call(fake_ctx, call_ctx, actual)

    @pytest.mark.asyncio
    async def test_timeout_error_propagates(self, fake_ctx):
        """AgentTimeoutError must propagate, not be swallowed."""
        exc = AgentTimeoutError("turn timed out")
        chain = InterceptorChain([ControlErrorInterceptor(exc)])
        call_ctx = _make_tool_call_ctx()

        async def actual() -> ToolResult:
            return ToolResult.from_text("test_tool", "ok")

        with pytest.raises(AgentTimeoutError):
            await chain.around_tool_call(fake_ctx, call_ctx, actual)

    @pytest.mark.asyncio
    async def test_agent_control_error_propagates(self, fake_ctx):
        """AgentControlError base class must propagate."""
        exc = AgentControlError("generic control")
        chain = InterceptorChain([ControlErrorInterceptor(exc)])
        call_ctx = _make_tool_call_ctx()

        async def actual() -> ToolResult:
            return ToolResult.from_text("test_tool", "ok")

        with pytest.raises(AgentControlError):
            await chain.around_tool_call(fake_ctx, call_ctx, actual)


class TestInterceptorChainOnionOrder:
    """Verify outermost interceptor enters first and exits last."""

    @pytest.mark.asyncio
    async def test_onion_wrapping_order(self, fake_ctx):
        log: list[str] = []
        chain = InterceptorChain([
            OrderInterceptor("outer", log),
            OrderInterceptor("inner", log),
        ])
        call_ctx = _make_tool_call_ctx()

        async def actual() -> ToolResult:
            log.append("actual")
            return ToolResult.from_text("test_tool", "ok")

        await chain.around_tool_call(fake_ctx, call_ctx, actual)

        assert log == ["outer_in", "inner_in", "actual", "inner_out", "outer_out"]

    @pytest.mark.asyncio
    async def test_shortcircuit_skips_inner_and_actual(self, fake_ctx):
        log: list[str] = []
        chain = InterceptorChain([
            OrderInterceptor("outer", log),
            ShortCircuitInterceptor(
                ToolResult.from_text("test_tool", "shortcut")
            ),
            OrderInterceptor("inner", log),
        ])
        call_ctx = _make_tool_call_ctx()

        async def actual() -> ToolResult:
            log.append("actual")
            return ToolResult.from_text("test_tool", "ok")

        result = await chain.around_tool_call(fake_ctx, call_ctx, actual)

        assert result.message_content() == "shortcut"
        assert log == ["outer_in", "outer_out"]
        assert "actual" not in log


class TestInterceptorChainTurn:
    """around_turn behaviour — plain exceptions propagate, control exceptions propagate."""

    @pytest.mark.asyncio
    async def test_turn_plain_exception_propagates(self, fake_ctx):
        class BoomTurnInterceptor(TurnInterceptor):
            @property
            def name(self) -> str:
                return "boom-turn"

            async def around_turn(self, ctx, next_call: TurnNext) -> AgentResult:
                raise RuntimeError("turn boom")

        chain = InterceptorChain([BoomTurnInterceptor()])

        async def actual() -> AgentResult:
            return AgentResult(content="ok")

        with pytest.raises(RuntimeError, match="turn boom"):
            await chain.around_turn(fake_ctx, actual)

    @pytest.mark.asyncio
    async def test_turn_control_error_propagates(self, fake_ctx):
        class CancelTurnInterceptor(TurnInterceptor):
            @property
            def name(self) -> str:
                return "cancel-turn"

            async def around_turn(self, ctx, next_call: TurnNext) -> AgentResult:
                raise AgentCancelledError("admin cancel")

        chain = InterceptorChain([CancelTurnInterceptor()])

        async def actual() -> AgentResult:
            return AgentResult(content="ok")

        with pytest.raises(AgentCancelledError):
            await chain.around_turn(fake_ctx, actual)


class TestInterceptorChainIteration:
    """around_iteration behaviour."""

    @pytest.mark.asyncio
    async def test_iteration_plain_exception_propagates(self, fake_ctx):
        class BoomIterationInterceptor(IterationInterceptor):
            @property
            def name(self) -> str:
                return "boom-iteration"

            async def around_iteration(self, ctx, call: IterationContext, next_call) -> None:
                raise RuntimeError("iteration boom")

        chain = InterceptorChain([BoomIterationInterceptor()])

        async def actual() -> None:
            pass

        with pytest.raises(RuntimeError, match="iteration boom"):
            await chain.around_iteration(fake_ctx, IterationContext(1, "t1"), actual)

    @pytest.mark.asyncio
    async def test_iteration_cancelled_propagates(self, fake_ctx):
        class CancelIterationInterceptor(IterationInterceptor):
            @property
            def name(self) -> str:
                return "cancel-iteration"

            async def around_iteration(self, ctx, call: IterationContext, next_call) -> None:
                raise AgentCancelledError("cancel")

        chain = InterceptorChain([CancelIterationInterceptor()])

        async def actual() -> None:
            pass

        with pytest.raises(AgentCancelledError):
            await chain.around_iteration(fake_ctx, IterationContext(1, "t1"), actual)
