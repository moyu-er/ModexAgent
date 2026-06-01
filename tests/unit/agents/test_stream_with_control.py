"""Tests for ReActAgent._stream_with_control — LLM_STREAM interceptor path."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from framework.agents.react.agent import ReActEvent, ReActAgent
from framework.core.provider import StreamingLLMProvider
from framework.core.types import LLMResponse, ToolCall
from framework.interceptor.abc import InterceptorScope


class _StreamingEmitter:
    """Emitter that wants streaming and captures events."""

    def __init__(self):
        self.events: list = []
        self.deltas: list[str] = []
        self._stream_end_resuming: bool | None = None

    def wants_streaming(self) -> bool:
        return True

    async def emit(self, event, data=None):
        self.events.append((event, data))

    async def emit_delta(self, delta: str):
        if delta:
            self.deltas.append(delta)

    async def emit_content(self, full: str):
        if full:
            self.deltas.append(full)

    async def emit_stream_end(self, resuming: bool = False):
        self._stream_end_resuming = resuming

    async def emit_complete(self, result):
        pass

    async def emit_error(self, error: str):
        pass


class _FakeHistory:
    def __init__(self):
        self.messages: list = []

    async def append(self, message):
        self.messages.append(message)

    async def replace_all(self, messages):
        self.messages = list(messages)

    def __iter__(self):
        return iter(self.messages)


def _make_fake_ctx(*, interceptor_chain=None):
    from framework.core.agent import AgentContext
    from framework.memory.history import ListMessageHistory
    from framework.core.tool_manager import InMemoryToolManager
    from framework.agents.react.state import ReActTurnState
    from framework.runtime.services import AgentRuntime, AgentRuntimeServices
    from framework.runtime.models import TurnIdentity
    from framework.runtime.enums import AgentKind, TurnPhase
    state = ReActTurnState(
        identity=TurnIdentity(agent_id="test", session_id="test-session-001", turn_id="t1"),
        agent_kind=AgentKind.REACT, phase=TurnPhase.CREATED,
    )
    runtime = AgentRuntime(services=AgentRuntimeServices(interceptors=interceptor_chain), state=state)
    ctx = AgentContext(
        system_prompt="", history=ListMessageHistory(),
        tool_manager=InMemoryToolManager(), session_id="test-session-001",
        max_iterations=3,
        identity=state.identity, runtime=runtime,
    )
    ctx.messages = [{"role": "user", "content": "run a tool"}]
    return ctx


class TestStreamWithControlPreservesToolCalls:
    """P0-1r2: _stream_with_control MUST preserve tool_calls from LLM response."""

    async def test_preserves_tool_calls_when_llm_returns_them(self):
        """When LLM returns tool_calls via chat_stream, _stream_with_control
        must include them in the returned LLMResponse."""
        # Arrange: mock interceptor_chain with has_scope(LLM_STREAM)=True
        from framework.interceptor.abc import LLMStreamChunk, LLMStreamContext

        async def _fake_llm_stream(ctx, call, actual_stream):
            """Mirror the actual stream, passing chunks through."""
            async for chunk in actual_stream():
                yield chunk

        fake_chain = MagicMock()

        async def _fake_around_turn(ctx, next_call):
            return await next_call()

        async def _fake_around_iteration(ctx, call, next_call):
            return await next_call()

        fake_chain.has_scope = MagicMock(return_value=True)
        fake_chain.around_turn = _fake_around_turn
        fake_chain.around_iteration = _fake_around_iteration
        fake_chain.around_llm_stream = _fake_llm_stream

        # Arrange: mock streaming provider that returns tool_calls
        tool_call = ToolCall(tool_name="bash", arguments={"cmd": "ls"}, call_id="call_1")

        class StreamingProvider(StreamingLLMProvider):
            async def chat_stream(self, messages, tools=None, temperature=0.7,
                                  max_tokens=None, on_content_delta=None,
                                  on_reasoning_delta=None, **kwargs):
                if on_content_delta:
                    await on_content_delta("Let me run a command.")
                return LLMResponse(
                    content="Let me run a command.",
                    finish_reason="tool_calls",
                    tool_calls=[tool_call],
                )

            async def chat(self, *args, **kwargs) -> LLMResponse:
                return LLMResponse(content="", finish_reason="stop")

            def get_default_model(self) -> str:
                return "test-model"

        provider = StreamingProvider()
        agent = ReActAgent(provider=provider)
        emitter = _StreamingEmitter()
        ctx = _make_fake_ctx(interceptor_chain=fake_chain)

        # Act
        result = await agent.run(ctx, emitter)

        # Assert: tool_calls MUST be in the result's messages
        # The assistant message in all_new_messages should contain tool_calls
        assert result.messages is not None, "Result should contain messages"
        assistant_msgs = [m for m in result.messages
                          if isinstance(m, dict) and m.get("role") == "assistant"
                          and m.get("tool_calls")]
        assert len(assistant_msgs) > 0, (
            f"Expected assistant message with tool_calls, got: {result.messages}"
        )

        # Assert: emitter should know this is a tool-call turn (not final)
        assert emitter._stream_end_resuming is True, (
            f"emit_stream_end should be called with resuming=True when tool_calls exist, "
            f"got resuming={emitter._stream_end_resuming}"
        )

    async def test_no_tool_calls_when_llm_returns_none(self):
        """When LLM returns NO tool_calls, _stream_with_control must work correctly."""
        from framework.interceptor.abc import LLMStreamChunk, LLMStreamContext

        async def _fake_llm_stream(ctx, call, actual_stream):
            async for chunk in actual_stream():
                yield chunk

        fake_chain = MagicMock()

        async def _fake_around_turn(ctx, next_call):
            return await next_call()

        async def _fake_around_iteration(ctx, call, next_call):
            return await next_call()

        fake_chain.has_scope = MagicMock(return_value=True)
        fake_chain.around_turn = _fake_around_turn
        fake_chain.around_iteration = _fake_around_iteration
        fake_chain.around_llm_stream = _fake_llm_stream

        class StreamingProviderNoTools(StreamingLLMProvider):
            async def chat_stream(self, messages, tools=None, temperature=0.7,
                                  max_tokens=None, on_content_delta=None,
                                  on_reasoning_delta=None, **kwargs):
                if on_content_delta:
                    await on_content_delta("Hello!")
                return LLMResponse(
                    content="Hello!",
                    finish_reason="stop",
                    tool_calls=[],
                )

            async def chat(self, *args, **kwargs) -> LLMResponse:
                return LLMResponse(content="", finish_reason="stop")

            def get_default_model(self) -> str:
                return "test-model"

        provider = StreamingProviderNoTools()
        agent = ReActAgent(provider=provider)
        emitter = _StreamingEmitter()
        ctx = _make_fake_ctx(interceptor_chain=fake_chain)

        result = await agent.run(ctx, emitter)

        assert result.content == "Hello!"
        assert emitter._stream_end_resuming is False
