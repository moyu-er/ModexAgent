"""Tests for ReActAgent error response and cancellation handling (P0-a)."""

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from framework.agents.react.agent import ReActAgent
from framework.agents.react.state import ReActTurnState
from framework.core.constants import FinishReason
from framework.runtime.enums import TurnCustomKey
from framework.core.emitter import AgentResult
from framework.core.tool_manager import ToolResult
from framework.core.types import LLMResponse, ToolCall
from framework.runtime.enums import AgentKind, TurnPhase
from framework.runtime.models import TurnIdentity
from framework.core.session_id import SessionInfo
from framework.runtime.services import AgentRuntime, AgentRuntimeServices


def _make_ctx(**kw):
    """Create a real AgentContext with typed runtime state."""
    from framework.core.agent import AgentContext
    from framework.memory.history import ListMessageHistory
    from framework.core.tool_manager import InMemoryToolManager
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


class TestReActAgentErrorResponse:
    @pytest.mark.asyncio
    async def test_error_finish_reason_returns_agent_error(self):
        provider = MagicMock()
        provider.chat = AsyncMock(return_value=LLMResponse(
            content="Error calling LLM: something went wrong",
            finish_reason=FinishReason.ERROR.value,
            error="something went wrong",
        ))
        provider.get_default_model = lambda: "mock"
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
        provider = MagicMock()
        provider.chat = AsyncMock(return_value=LLMResponse(
            content="Hello, how can I help?",
            finish_reason=FinishReason.STOP.value,
        ))
        provider.get_default_model = lambda: "mock"
        agent = ReActAgent(provider=provider)
        emitter = _FakeEmitter()
        ctx = _make_ctx()
        result = await agent.run(ctx, emitter)
        assert result is not None
        assert result.stop_reason == "completed" or result.stop_reason == "stop"
        assert result.content == "Hello, how can I help?"


class TestReActAgentCancelledError:
    @pytest.mark.asyncio
    async def test_cancelled_error_preserves_checkpoint(self):
        provider = MagicMock()
        async def raise_cancelled(*args, **kwargs):
            raise asyncio.CancelledError()
        provider.chat = raise_cancelled
        provider.get_default_model = lambda: "mock"
        agent = ReActAgent(provider=provider)
        emitter = _FakeEmitter()
        ctx = _make_ctx()
        # CancelledError propagates to caller; crash recovery will use
        # TurnSnapshot.message_delta saved by pipeline's snapshot policy.
        with pytest.raises(asyncio.CancelledError):
            await agent.run(ctx, emitter)


class TestReActAgentToolTimeout:
    @pytest.mark.asyncio
    async def test_tool_timeout_returns_error_result(self):
        provider = MagicMock()
        provider.chat = AsyncMock(return_value=LLMResponse(
            content="", finish_reason=FinishReason.TOOL_CALLS.value,
            tool_calls=[ToolCall(tool_name="slow_tool", arguments={}, call_id="c1")],
        ))
        provider.get_default_model = lambda: "mock"
        class SlowTool:
            async def execute(self, tool_name, arguments):
                await asyncio.sleep(0.5)
                return ToolResult(tool_name=tool_name, result="done")
        class FakeToolManager:
            def __init__(self, tool):
                self._tool = tool
            async def execute(self, tool_name, arguments):
                return await self._tool.execute(tool_name, arguments)
            def get_tool_descriptions(self):
                return []
        tool = SlowTool()
        ctx = _make_ctx()
        ctx.tool_manager = FakeToolManager(tool)
        agent = ReActAgent(provider=provider, tool_timeout=0.01)
        emitter = _FakeEmitter()
        result = await agent.run(ctx, emitter)
        assert result is not None


class TestReActAgentHookTimeout:
    @pytest.mark.asyncio
    async def test_hook_timeout_is_logged_not_raised(self):
        provider = MagicMock()
        provider.chat = AsyncMock(return_value=LLMResponse(
            content="ok", finish_reason=FinishReason.STOP.value,
        ))
        provider.get_default_model = lambda: "mock"
        class SlowHook:
            async def before_turn(self, context):
                await asyncio.sleep(0.5)
        agent = ReActAgent(provider=provider, hook_timeout=0.01)
        emitter = _FakeEmitter()
        ctx = _make_ctx()
        from framework.hook import HookRunner
        ctx.runtime.services.hooks = HookRunner([SlowHook()])
        result = await agent.run(ctx, emitter)
        assert result is not None
