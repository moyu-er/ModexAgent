"""Tests for ReActAgent._stream_with_control — LLM_STREAM interceptor path.

Includes mid-turn cancel verification: a CANCEL_TURN injected while the LLM
is streaming must be consumed by the LlmCancelInterceptor and abort the turn.
"""

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from modex_agent.agents.react.agent import ReActEvent, ReActAgent
from modex_agent.core.provider import StreamingLLMProvider
from modex_agent.core.types import LLMResponse, ToolCall
from modex_agent.interceptor.abc import InterceptorScope


class _StreamingEmitter:
    """Emitter that wants streaming and captures events."""

    def __init__(self):
        self.events: list = []
        self.deltas: list[str] = []
        self._stream_end_resuming: bool | None = None
        self.completed: Any = None
        self.error: str | None = None

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
        self.completed = result

    async def emit_error(self, error: str):
        self.error = error


def _make_fake_ctx(*, interceptor_chain=None, control_channel=None):
    from modex_agent.core.agent import AgentContext
    from modex_agent.memory.history import ListMessageHistory
    from modex_agent.core.tool_manager import InMemoryToolManager
    from modex_agent.agents.react.state import ReActTurnState
    from modex_agent.runtime.services import AgentRuntime, AgentRuntimeServices
    from modex_agent.runtime.models import TurnIdentity
    from modex_agent.runtime.enums import AgentKind, TurnPhase
    from modex_agent.core.session_id import SessionInfo
    state = ReActTurnState(
        identity=TurnIdentity(agent_id="test", session=SessionInfo.from_str("test-session-001"), turn_id="t1"),
        agent_kind=AgentKind.REACT, phase=TurnPhase.CREATED,
    )
    services = AgentRuntimeServices(
        interceptors=interceptor_chain,
        control_channel=control_channel,
    )
    runtime = AgentRuntime(services=services, state=state)
    ctx = AgentContext(
        system_prompt="", history=ListMessageHistory(),
        tool_manager=InMemoryToolManager(), session=SessionInfo.from_str("test.agent"),
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
        from modex_agent.interceptor.abc import LLMStreamChunk, LLMStreamContext

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
        from modex_agent.interceptor.abc import LLMStreamChunk, LLMStreamContext

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


class TestMidTurnCancelViaInterceptor:
    """CANCEL_TURN injected while a turn is in-flight must be consumed by the
    LlmCancelInterceptor and abort the turn cleanly.

    Without this, the WebUI pause button sends CANCEL_TURN but it sits in the
    channel unconsumed — the LLM streams to completion, tools execute, and
    the turn finishes naturally.  The user observes "点击暂停→毫无反应".
    """

    @pytest.mark.asyncio
    async def test_mid_stream_cancel_via_callback_drain(self):
        """CANCEL_TURN injected BEFORE the LLM stream begins must be consumed
        by the drain inside _on_content_delta and abort the turn immediately —
        before chat_stream returns.  This is the fast path: one content delta
        fires, the drain finds CANCEL_TURN, the provider aborts the stream."""
        from modex_agent.control.channel import InMemoryControlChannel
        from modex_agent.control.types import (
            ControlCommand,
            ControlCommandType,
            ControlScope,
        )
        from modex_agent.interceptor.chain import InterceptorChain

        channel = InMemoryControlChannel()
        chain = InterceptorChain()

        # Pre-load CANCEL_TURN so the first _on_content_delta drain finds it.
        await channel.send(ControlCommand(
            command_id="cancel-preloaded",
            type=ControlCommandType.CANCEL_TURN,
            scope=ControlScope(session_id="test.agent"),
        ))

        class CancellableStreamProvider(StreamingLLMProvider):
            async def chat_stream(self, messages, tools=None, temperature=0.7,
                                  max_tokens=None, on_content_delta=None,
                                  on_reasoning_delta=None, **kwargs):
                if on_content_delta:
                    # This callback drain will find and consume CANCEL_TURN,
                    # raising AgentCancelled which propagates through the
                    # provider back to _stream_with_control.
                    await on_content_delta("shall be cancelled")
                # Should never reach here.
                return LLMResponse(
                    content="", finish_reason="stop", tool_calls=[],
                )

            async def chat(self, *args, **kwargs) -> LLMResponse:
                return LLMResponse(content="", finish_reason="stop")

            def get_default_model(self) -> str:
                return "test-model"

        provider = CancellableStreamProvider()
        agent = ReActAgent(provider=provider)
        emitter = _StreamingEmitter()
        ctx = _make_fake_ctx(interceptor_chain=chain, control_channel=channel)

        result = await agent.run(ctx, emitter)

        assert emitter.completed is not None, (
            "Drain inside _on_content_delta must raise AgentCancelled "
            "immediately, and ReActAgent must emit turn_end."
        )
        assert result.stop_reason == "cancelled"

    @pytest.mark.asyncio
    async def test_mid_stream_cancel_aborts_turn(self):
        from modex_agent.control.channel import InMemoryControlChannel
        from modex_agent.control.types import (
            ControlCommand,
            ControlCommandType,
            ControlScope,
        )
        from modex_agent.hook.builtin.control_drain import LlmCancelInterceptor
        from modex_agent.interceptor.chain import InterceptorChain

        channel = InMemoryControlChannel()
        chain = InterceptorChain()
        chain.add(LlmCancelInterceptor(channel=channel))

        # Slow provider: emits one chunk, then waits for external signal.
        # The test body injects CANCEL_TURN while chat_stream is "in-flight".
        chunk_gate = asyncio.Event()

        class SlowStreamingProvider(StreamingLLMProvider):
            async def chat_stream(self, messages, tools=None, temperature=0.7,
                                  max_tokens=None, on_content_delta=None,
                                  on_reasoning_delta=None, **kwargs):
                if on_content_delta:
                    await on_content_delta("正在分析问题...")
                # Simulate a long-running LLM call — the user clicks pause
                # during this window.
                await chunk_gate.wait()
                return LLMResponse(
                    content="这是完整的回答。",
                    finish_reason="stop",
                    tool_calls=[],
                )

            async def chat(self, *args, **kwargs) -> LLMResponse:
                return LLMResponse(content="", finish_reason="stop")

            def get_default_model(self) -> str:
                return "test-model"

        provider = SlowStreamingProvider()
        agent = ReActAgent(provider=provider)
        emitter = _StreamingEmitter()
        ctx = _make_fake_ctx(interceptor_chain=chain, control_channel=channel)

        # Kick off the turn (it will block inside chat_stream at chunk_gate).
        turn_task = asyncio.create_task(agent.run(ctx, emitter))

        # Wait for the first delta to confirm streaming has started.
        await asyncio.sleep(0.2)
        assert len(emitter.deltas) >= 1, "streaming must have started"

        # Inject CANCEL_TURN now (simulating the user clicking pause mid-stream).
        await channel.send(ControlCommand(
            command_id="cancel-mid",
            type=ControlCommandType.CANCEL_TURN,
            scope=ControlScope(session_id="test.agent"),
        ))

        # Unblock the provider so chat_stream returns and the interceptor
        # drains the control channel.
        chunk_gate.set()

        result = await asyncio.wait_for(turn_task, timeout=10.0)

        assert emitter.completed is not None, (
            "LlmCancelInterceptor must raise AgentCancelled after draining the "
            "channel, and ReActAgent must emit turn_end (emit_complete). "
            "Currently the CANCEL_TURN is silently consumed OR the turn "
            "completes normally — the pause button has no effect."
        )
        assert result.stop_reason == "cancelled", (
            f"Expected stop_reason='cancelled', got '{result.stop_reason}'. "
            "The turn must know it was cancelled, not completed normally."
        )
