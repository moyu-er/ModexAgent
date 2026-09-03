"""Tests for ReActAgent error response and cancellation handling (P0-a)."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from modex_agent.agents.react.agent import ReActAgent
from modex_agent.agents.react.state import ReActTurnState
from modex_agent.core.emitter import AgentResult, StopReason
from modex_agent.core.llm_struct import FinishReason, LLMResponse
from modex_agent.core.message import ToolCall
from modex_agent.core.provider import CallbackStreamProvider
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.tool_manager import ToolResult
from modex_agent.runtime.enums import AgentKind, TurnPhase
from modex_agent.runtime.models import TurnIdentity
from modex_agent.runtime.services import AgentRuntime, AgentRuntimeServices

if TYPE_CHECKING:
    from modex_agent.core.capabilities import ModelCapabilities


def _make_ctx(**kw):
    """Create a real AgentContext with typed runtime state."""
    from modex_agent.core.agent import AgentContext
    from modex_agent.memory.history import ListMessageHistory
    from modex_agent.tools.manager import InMemoryToolManager
    state = ReActTurnState(
        identity=TurnIdentity(agent_id="test", session=SessionInfo.from_str("s1"), turn_id="t1"),
        agent_kind=AgentKind.REACT, phase=TurnPhase.CREATED,
    )
    runtime = AgentRuntime(services=AgentRuntimeServices(), state=state)
    return AgentContext(
        system_prompt="", history=ListMessageHistory(),
        tool_manager=InMemoryToolManager(), session=SessionInfo.from_str("test.agent"),
        max_iterations=5,
        identity=state.identity, runtime=runtime,
        **kw,
    )


class _FakeEmitter:
    def __init__(self):
        self.events: list[tuple] = []
        self.deltas: list[str] = []
        self.completed: AgentResult | None = None
        self._streaming = False

    def wants_streaming(self) -> bool:
        return self._streaming

    async def emit(self, event, data=None):
        self.events.append((event, data))

    async def emit_delta(self, delta: str):
        self.deltas.append(delta)

    async def emit_content(self, full: str):
        if full:
            self.deltas.append(full)

    async def emit_stream_end(self, resuming: bool = False):
        pass

    async def emit_complete(self, result: AgentResult):
        self.completed = result

    async def emit_error(self, error: str):
        self.events.append(("error", error))


class _ScriptedProvider(CallbackStreamProvider):
    """chat-only scripted mock riding the callback→event bridge."""

    def __init__(self, response: LLMResponse | None = None, error: BaseException | None = None):
        super().__init__()
        self._response = response
        self._error = error

    def get_default_model(self) -> str:
        return "mock"

    async def chat_stream(self, messages, *, on_content_delta=None, on_reasoning_delta=None, **kw):
        if self._error is not None:
            raise self._error
        assert self._response is not None
        return self._response


class TestReActAgentErrorResponse:
    @pytest.mark.asyncio
    async def test_error_finish_reason_returns_agent_error(self):
        provider = _ScriptedProvider(response=LLMResponse(
            content="Error calling LLM: something went wrong",
            finish_reason=FinishReason.ERROR.value,
            error="something went wrong",
        ))
        agent = ReActAgent(provider=provider)
        emitter = _FakeEmitter()
        ctx = _make_ctx()

        result = await agent.run(ctx, emitter)
        assert result is not None
        assert result.stop_reason == "error"
        assert result.error is not None
        assert "something went wrong" in result.error or "LLM request failed" in result.error

    @pytest.mark.asyncio
    async def test_normal_response_proceeds(self):
        provider = _ScriptedProvider(response=LLMResponse(
            content="Hello, how can I help?",
            finish_reason=FinishReason.STOP.value,
        ))
        agent = ReActAgent(provider=provider)
        emitter = _FakeEmitter()
        ctx = _make_ctx()
        result = await agent.run(ctx, emitter)
        assert result is not None
        assert result.stop_reason == "completed" or result.stop_reason == "stop"
        assert result.content == "Hello, how can I help?"


class TestReActAgentCancelledError:
    @pytest.mark.asyncio
    async def test_cancelled_error_returns_cancelled_result(self):
        """CancelledError (e.g. from task.cancel() mid-stream) must emit
        a terminal signal and return a cancelled result, not crash the turn.

        Previously CancelledError re-raised out of the agent and was treated
        as a dispatch error. The control-channel CANCEL_TURN now uses
        task.cancel() to interrupt in-flight LLM calls, so the agent must
        handle CancelledError cleanly.
        """
        provider = _ScriptedProvider(error=asyncio.CancelledError())
        agent = ReActAgent(provider=provider)
        emitter = _FakeEmitter()
        ctx = _make_ctx()
        result = await agent.run(ctx, emitter)

        assert emitter.completed is not None, (
            "CancelledError must emit a terminal signal (emit_complete) "
            "so the frontend is not stuck streaming."
        )
        assert result is not None
        assert result.stop_reason == StopReason.CANCELLED


class TestReActAgentControlCancel:
    """Control-driven cancel (CANCEL_TURN via control channel) must send a
    terminal signal to the emitter so downstream consumers (e.g. the WebUI
    turn_end event) learn the turn ended.

    Regression: the ``except AgentControlError`` branch re-raised without
    calling ``emit_complete``, so a cancelled turn never produced turn_end and
    the WebUI pause button appeared to do nothing (frontend stuck streaming).
    """

    @pytest.mark.asyncio
    async def test_control_cancel_emits_turn_end(self):
        from modex_agent.control.channel import InMemoryControlChannel
        from modex_agent.control.types import (
            ControlCommand,
            ControlCommandType,
            ControlScope,
        )

        # Pre-load a CANCEL_TURN for this session. The command carries no
        # turn_uuid, so the turn-start drain executes it immediately
        # (backward-compatible defense in drain_control_channel).
        channel = InMemoryControlChannel()
        await channel.send(ControlCommand(
            command_id="cancel-1",
            type=ControlCommandType.CANCEL_TURN,
            scope=ControlScope(session_id="test.agent"),
        ))

        provider = _ScriptedProvider(response=LLMResponse(
            content="unreached", finish_reason=FinishReason.STOP.value,
        ))

        agent = ReActAgent(provider=provider)
        emitter = _FakeEmitter()
        ctx = _make_ctx()
        ctx.runtime.services.control_channel = channel

        # The cancel is controlled, not a crash: it must not escape as an
        # unhandled exception, and it must emit a terminal signal.
        result = await agent.run(ctx, emitter)

        assert emitter.completed is not None, (
            "CANCEL_TURN must emit a terminal signal (emit_complete) so the "
            "turn ends cleanly; otherwise the WebUI pause leaves the frontend "
            "stuck streaming."
        )
        assert result is not None
        assert result.stop_reason == StopReason.CANCELLED


class TestReActAgentToolTimeout:
    @pytest.mark.asyncio
    async def test_tool_timeout_returns_error_result(self):
        provider = _ScriptedProvider(response=LLMResponse(
            content="", finish_reason=FinishReason.TOOL_CALLS.value,
            tool_calls=[ToolCall(tool_name="slow_tool", arguments={}, call_id="c1")],
        ))
        class SlowTool:
            async def execute(self, tool_name, arguments):
                await asyncio.sleep(0.5)
                return ToolResult.from_text(tool_name, "done")
        class FakeToolManager:
            def __init__(self, tool):
                self._tool = tool
            async def execute(self, tool_name, arguments):
                return await self._tool.execute(tool_name, arguments)
            def get_tool_descriptions(self, caps: ModelCapabilities | None = None):
                return []
        tool = SlowTool()
        ctx = _make_ctx()
        ctx.tool_manager = FakeToolManager(tool)
        from modex_agent.core.llm_struct import RuntimeSafetyPolicy, TurnTimeoutPolicy
        ctx.runtime.services.safety = RuntimeSafetyPolicy(
            turn=TurnTimeoutPolicy(tool_timeout_seconds=0.01),
        )
        agent = ReActAgent(provider=provider)
        emitter = _FakeEmitter()
        result = await agent.run(ctx, emitter)
        assert result is not None


class TestReActAgentHookDispatch:
    @pytest.mark.asyncio
    async def test_slow_hook_does_not_abort_turn(self):
        provider = _ScriptedProvider(response=LLMResponse(
            content="ok", finish_reason=FinishReason.STOP.value,
        ))
        class SlowHook:
            async def before_turn(self, context):
                await asyncio.sleep(0.5)
        agent = ReActAgent(provider=provider)
        emitter = _FakeEmitter()
        ctx = _make_ctx()
        from modex_agent.hook import HookRunner
        ctx.runtime.services.hooks = HookRunner([SlowHook()])
        result = await agent.run(ctx, emitter)
        assert result is not None
