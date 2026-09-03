"""ReactLlmClient single event loop — emitter timing equivalence + assembly.

Locks the ADR-0046 event-loop contract (PRD §10):
- the emitter call sequence is equivalent to the legacy callback loop:
  emit_delta + emit(MODEL_OUTPUT) per content delta, emit(MODEL_REASONING)
  per reasoning delta, emit_stream_end(resuming=has_tool_calls) at the tail;
- bridged legacy mocks (only chat_stream overridden) keep tool_calls alive
  so the ReAct loop never breaks on a bridged path;
- stream-native event sequences assemble into LLMResponse, replay
  fields included;
- mid-stream cancels stash INTERRUPTED_PARTIAL with the streamed content
  and re-raise;
- LlmCancelInterceptor's hard cancel propagates out of the chain;
- non-streaming emitters ride the same loop with zero emitter calls.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from modex_agent.agents.react.agent import ReActEvent
from modex_agent.agents.react.llm_client import ReactLlmClient
from modex_agent.agents.react.state import ReActTurnState
from modex_agent.control.channel import InMemoryControlChannel
from modex_agent.control.exceptions import AgentCancelledError
from modex_agent.control.types import ControlCommand, ControlCommandType, ControlScope
from modex_agent.core.constants import FinishReason
from modex_agent.core.llm_request import LLMRequest
from modex_agent.core.llm_struct import LLMErrorInfo, LLMErrorKind
from modex_agent.core.provider import CallbackStreamProvider, LLMProvider
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.stream_events import (
    Finish,
    LLMStreamEvent,
    ReasoningDelta,
    ReplayFields,
    StreamFailure,
    TextDelta,
    ToolCallComplete,
    UsageSnapshot,
)
from modex_agent.core.types import LLMResponse, TokenUsage, ToolCall
from modex_agent.hook.builtin.control_drain import LlmCancelInterceptor
from modex_agent.interceptor.abc import LLMStreamInterceptor
from modex_agent.interceptor.chain import InterceptorChain
from modex_agent.memory.history import ListMessageHistory
from modex_agent.runtime.enums import AgentKind, TurnCustomKey, TurnPhase
from modex_agent.runtime.models import TurnIdentity
from modex_agent.runtime.services import AgentRuntime, AgentRuntimeServices
from modex_agent.tools.manager import InMemoryToolManager


def _make_ctx():
    from modex_agent.core.agent import AgentContext
    from modex_agent.tools.manager import InMemoryToolManager

    state = ReActTurnState(
        identity=TurnIdentity(agent_id="test", session=SessionInfo.from_str("s1"), turn_id="t1"),
        agent_kind=AgentKind.REACT,
        phase=TurnPhase.CREATED,
    )
    runtime = AgentRuntime(services=AgentRuntimeServices(), state=state)
    return AgentContext(
        system_prompt="",
        history=ListMessageHistory(),
        tool_manager=InMemoryToolManager(),
        session=SessionInfo.from_str("test.agent"),
        max_iterations=5,
        identity=state.identity,
        runtime=runtime,
    )


class _RecordingEmitter:
    """Records every emitter call as (method, *args) tuples."""

    def __init__(self, streaming: bool = True):
        self._streaming = streaming
        self.calls: list[tuple[object, ...]] = []

    def wants_streaming(self) -> bool:
        return self._streaming

    async def emit(self, event, data=None):
        self.calls.append(("emit", event, data))

    async def emit_delta(self, delta: str) -> None:
        self.calls.append(("emit_delta", delta))

    async def emit_content(self, full_content: str) -> None:
        self.calls.append(("emit_content", full_content))

    async def emit_stream_end(self, resuming: bool = False) -> None:
        self.calls.append(("emit_stream_end", resuming))

    async def emit_complete(self, result) -> None:
        self.calls.append(("emit_complete", result))

    async def emit_error(self, error: str) -> None:
        self.calls.append(("emit_error", error))


class _LegacyCallbackProvider(CallbackStreamProvider):
    """Callback-style provider: only overrides chat_stream (bridge path)."""

    def __init__(
        self,
        deltas: list[str] | None = None,
        reasoning: list[str] | None = None,
        response: LLMResponse | None = None,
    ):
        self._deltas = deltas or []
        self._reasoning = reasoning or []
        self._response = response

    def get_default_model(self) -> str:
        return "mock"

    async def chat(self, messages, **kw):  # pragma: no cover - bridge never calls it
        raise RuntimeError("not used")

    async def chat_stream(self, messages, *, on_content_delta, on_reasoning_delta, **kw):
        for delta in self._deltas:
            await on_content_delta(delta)
        for delta in self._reasoning:
            await on_reasoning_delta(delta)
        if self._response is not None:
            return self._response
        return LLMResponse(content="".join(self._deltas), finish_reason="stop")


class _CancellingLegacyProvider(CallbackStreamProvider):
    """Callback-style provider: chat_stream raises CancelledError (bridge path)."""

    def __init__(self, deltas: list[str] | None = None):
        self._deltas = deltas or []

    def get_default_model(self) -> str:
        return "mock"

    async def chat(self, messages, **kw):  # pragma: no cover - bridge never calls it
        raise RuntimeError("not used")

    async def chat_stream(self, messages, *, on_content_delta, on_reasoning_delta, **kw):
        for delta in self._deltas:
            await on_content_delta(delta)
        raise asyncio.CancelledError()


class TestEmitterTimingEquivalence:
    """The event loop's emitter call sequence must equal the legacy loop's."""

    async def test_call_sequence_matches_legacy_callback_loop(self):
        ctx = _make_ctx()
        emitter = _RecordingEmitter()
        ctx.emitter = emitter
        provider = _LegacyCallbackProvider(
            deltas=["Hello", " World"],
            reasoning=["thinking"],
        )

        result = await ReactLlmClient(provider).call([], ctx)

        assert emitter.calls == [
            ("emit_delta", "Hello"),
            ("emit", ReActEvent.MODEL_OUTPUT, "Hello"),
            ("emit_delta", " World"),
            ("emit", ReActEvent.MODEL_OUTPUT, " World"),
            ("emit", ReActEvent.MODEL_REASONING, "thinking"),
            ("emit_stream_end", False),
        ]
        assert result.content == "Hello World"
        assert result.reasoning_content == "thinking"

    async def test_stream_end_resuming_when_tool_calls_present(self):
        ctx = _make_ctx()
        emitter = _RecordingEmitter()
        ctx.emitter = emitter
        provider = _LegacyCallbackProvider(
            deltas=["Let me check..."],
            response=LLMResponse(
                content="Let me check...",
                tool_calls=[ToolCall(tool_name="bash", arguments={"cmd": "ls"}, call_id="c1")],
                finish_reason=FinishReason.TOOL_CALLS,
            ),
        )

        result = await ReactLlmClient(provider).call([], ctx)

        assert emitter.calls[-1] == ("emit_stream_end", True)
        assert emitter.calls[0] == ("emit_delta", "Let me check...")
        assert result.tool_calls is not None and len(result.tool_calls) == 1


class TestBridgedToolCallsFidelity:
    """tool_calls live only on the chat_stream return value — the bridge must
    re-translate them as ToolCallComplete events or the ReAct loop breaks."""

    async def test_tool_calls_survive_the_bridge(self):
        ctx = _make_ctx()
        emitter = _RecordingEmitter()
        ctx.emitter = emitter
        provider = _LegacyCallbackProvider(
            response=LLMResponse(
                content="",
                tool_calls=[ToolCall(tool_name="read_file", arguments={"path": "a"}, call_id="c1")],
                finish_reason=FinishReason.TOOL_CALLS,
            ),
        )

        result = await ReactLlmClient(provider).call([], ctx)

        assert [tc.tool_name for tc in result.tool_calls] == ["read_file"]
        assert result.tool_calls[0].arguments == {"path": "a"}
        assert emitter.calls[-1] == ("emit_stream_end", True)


class TestStreamNativeProviderAssembly:
    """Stream-native providers yield events directly — the loop assembles them."""

    async def test_event_sequence_assembles_full_response(self):
        class _DirectEventProvider(LLMProvider):
            def __init__(self, events: list[LLMStreamEvent]):
                self._events = events

            def get_default_model(self) -> str:
                return "mock"

            def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
                async def _gen() -> AsyncIterator[LLMStreamEvent]:
                    for event in self._events:
                        yield event

                return _gen()

        ctx = _make_ctx()
        emitter = _RecordingEmitter()
        ctx.emitter = emitter
        provider = _DirectEventProvider(
            [
                ReasoningDelta(text="step 1"),
                TextDelta(text="Hi"),
                ToolCallComplete(call_id="c1", tool_name="bash", arguments={"cmd": "ls"}),
                UsageSnapshot(usage=TokenUsage(input_tokens=5, output_tokens=2)),
                Finish(
                    finish_reason=FinishReason.TOOL_CALLS,
                    replay=ReplayFields(
                        reasoning_content="step 1",
                        reasoning_signature="sig-1",
                        reasoning_item_id="item-9",
                        reasoning_encrypted_content="enc-1",
                    ),
                ),
            ]
        )

        result = await ReactLlmClient(provider).call([], ctx)

        assert result.content == "Hi"
        assert result.reasoning_content == "step 1"
        assert result.reasoning_signature == "sig-1"
        assert result.reasoning_item_id == "item-9"
        assert result.reasoning_encrypted_content == "enc-1"
        assert [tc.tool_name for tc in result.tool_calls] == ["bash"]
        assert result.usage == TokenUsage(input_tokens=5, output_tokens=2)
        assert result.finish_reason == FinishReason.TOOL_CALLS
        # Reasoning delta emitted before its content delta, per event order.
        assert emitter.calls[0] == ("emit", ReActEvent.MODEL_REASONING, "step 1")
        assert emitter.calls[1] == ("emit_delta", "Hi")

    async def test_stream_failure_assembles_error_response(self):
        class _FailingEventProvider(LLMProvider):
            def get_default_model(self) -> str:
                return "mock"

            def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
                async def _gen() -> AsyncIterator[LLMStreamEvent]:
                    yield TextDelta(text="partial")
                    yield StreamFailure(
                        error_info=LLMErrorInfo(kind=LLMErrorKind.TIMEOUT, message="idle timeout"),
                        partial_content="",
                    )

                return _gen()

        ctx = _make_ctx()
        ctx.emitter = _RecordingEmitter()
        provider = _FailingEventProvider()

        result = await ReactLlmClient(provider).call([], ctx)

        assert result.finish_reason == FinishReason.ERROR
        assert result.error == "idle timeout"
        assert result.content == "partial"


class TestMidStreamCancelStashesPartial:
    """A cancel after some events must stash the streamed content and re-raise."""

    async def test_cancelled_error_after_second_event(self):
        class _CancellingEventProvider(LLMProvider):
            def get_default_model(self) -> str:
                return "mock"

            def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
                async def _gen() -> AsyncIterator[LLMStreamEvent]:
                    yield TextDelta(text="partial ")
                    yield TextDelta(text="content")
                    raise asyncio.CancelledError()

                return _gen()

        ctx = _make_ctx()
        ctx.emitter = _RecordingEmitter()
        provider = _CancellingEventProvider()

        with pytest.raises(asyncio.CancelledError):
            await ReactLlmClient(provider).call([], ctx)

        partial = ctx.runtime.state.custom.get(TurnCustomKey.INTERRUPTED_PARTIAL)
        assert partial == {"content": "partial content", "tool_names": []}

    async def test_agent_cancelled_error_from_chain_stashes_partial(self):
        class _CancellingChain:
            def has_scope(self, scope) -> bool:
                return True

            async def around_llm_stream(
                self, ctx, call, events: AsyncIterator[LLMStreamEvent]
            ) -> AsyncIterator[LLMStreamEvent]:
                async for event in events:
                    if event.kind == "reasoning_delta":
                        raise AgentCancelledError("mid-stream stop")
                    yield event

        ctx = _make_ctx()
        ctx.emitter = _RecordingEmitter()
        ctx.runtime.services.interceptors = _CancellingChain()
        provider = _LegacyCallbackProvider(deltas=["answer"], reasoning=["th"])

        with pytest.raises(AgentCancelledError):
            await ReactLlmClient(provider).call([], ctx)

        partial = ctx.runtime.state.custom.get(TurnCustomKey.INTERRUPTED_PARTIAL)
        assert partial == {"content": "answer", "tool_names": []}


class TestBridgeTranslatedCancel:
    """chat_stream raising CancelledError arrives (via the ADR-0046 callback
    bridge) as a terminal Finish(CANCELLED) event — the loop must translate it
    back into asyncio.CancelledError. Two producers, one consumer contract:
    the native producer (raise inside stream()) is covered by
    test_cancelled_error_after_second_event above."""

    async def test_immediate_bridge_cancel_raises_and_stashes_nothing(self):
        ctx = _make_ctx()
        ctx.emitter = _RecordingEmitter()
        provider = _CancellingLegacyProvider()

        with pytest.raises(asyncio.CancelledError):
            await ReactLlmClient(provider).call([], ctx)

        assert ctx.runtime.state.custom.get(TurnCustomKey.INTERRUPTED_PARTIAL) is None

    async def test_bridge_cancel_after_partial_stashes_streamed_content(self):
        ctx = _make_ctx()
        ctx.emitter = _RecordingEmitter()
        provider = _CancellingLegacyProvider(deltas=["partial "])

        with pytest.raises(asyncio.CancelledError):
            await ReactLlmClient(provider).call([], ctx)

        partial = ctx.runtime.state.custom.get(TurnCustomKey.INTERRUPTED_PARTIAL)
        assert partial == {"content": "partial ", "tool_names": []}


class TestLlmCancelInterceptorHardCancel:
    """drain 遇 CANCEL_TURN 抛 AgentCancelledError —— 原样向上传播(硬取消)。"""

    async def test_cancel_command_propagates_through_real_chain(self):
        channel = InMemoryControlChannel()
        await channel.send(
            ControlCommand(
                command_id="cancel-1",
                type=ControlCommandType.CANCEL_TURN,
                scope=ControlScope(session_id="test.agent"),
            )
        )
        chain = InterceptorChain()
        chain.add(LlmCancelInterceptor(channel=channel))

        ctx = _make_ctx()
        ctx.emitter = _RecordingEmitter()
        ctx.runtime.services.interceptors = chain
        ctx.runtime.services.control_channel = channel
        provider = _LegacyCallbackProvider(deltas=["never seen by the user"])

        with pytest.raises(AgentCancelledError):
            await ReactLlmClient(provider).call([], ctx)

        # Nothing streamed out and nothing stashed — the interceptor drains
        # before the first yield, so the loop never sees an event.
        assert ctx.runtime.state.custom.get(TurnCustomKey.INTERRUPTED_PARTIAL) is None
        # The command was consumed (destructive drain).
        remaining = await channel.peek(ControlScope(session_id="test.agent"))
        assert remaining == []


class TestChainExceptionTranslation:
    """链内拦截器的普通异常 → 合成 StreamFailure 终结事件后终止(不向上抛)。"""

    async def test_generic_interceptor_error_becomes_error_response(self):
        class _ExplodingInterceptor(LLMStreamInterceptor):
            @property
            def name(self) -> str:
                return "exploding"

            async def around_llm_stream(
                self, ctx, call, events: AsyncIterator[LLMStreamEvent]
            ) -> AsyncIterator[LLMStreamEvent]:
                async for event in events:
                    yield event
                    raise RuntimeError("boom inside interceptor")

        chain = InterceptorChain()
        chain.add(_ExplodingInterceptor())

        ctx = _make_ctx()
        emitter = _RecordingEmitter()
        ctx.emitter = emitter
        ctx.runtime.services.interceptors = chain
        provider = _LegacyCallbackProvider(deltas=["partial text"])

        result = await ReactLlmClient(provider).call([], ctx)

        assert result.finish_reason == FinishReason.ERROR
        assert "boom inside interceptor" in (result.error or "")
        assert result.content == "partial text"


class TestNonStreamingEmitterRidesSameLoop:
    """wants_streaming()==False → no per-delta emits; the folded response is
    delivered once at end-of-call (the legacy plain-chat contract)."""

    async def test_end_of_call_content_pair_but_result_assembled(self):
        ctx = _make_ctx()
        emitter = _RecordingEmitter(streaming=False)
        ctx.emitter = emitter
        provider = _LegacyCallbackProvider(deltas=["a", "b"])

        result = await ReactLlmClient(provider).call([], ctx)

        assert emitter.calls == [
            ("emit_content", "ab"),
            ("emit", ReActEvent.MODEL_OUTPUT, "ab"),
            ("emit_stream_end", False),
        ]
        assert result.content == "ab"
