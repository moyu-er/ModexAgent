"""Tests for ReactLlmClient.call's control-drain path.

Migrated from tests/unit/agents/test_react_agent_interrupted_partial.py —
the partial-stash WRITE belongs to ReactLlmClient._stream_with_control (the
client). The agent-level READ/persist (_persist_interrupted_partial) and the
interrupt-reason mapping remain tested at the agent level (see
test_react_agent_interrupted_partial.py).

Regression: on mid-stream cancel/pause, the assistant message append at the LLM
node (ctx.history.append) never ran, so memory lost the partial content while the
transcript kept it. The fix stashes the partial content in
``TurnCustomKey.INTERRUPTED_PARTIAL`` from ``_stream_with_control`` (now in the
client) and persists an XML-marked interrupted message in the agent's
cancel/error handlers.
"""

from collections.abc import AsyncIterator
from unittest.mock import MagicMock

import pytest

from modex_agent.agents.react.llm_client import ReactLlmClient
from modex_agent.agents.react.state import ReActTurnState
from modex_agent.control.exceptions import AgentCancelledError
from modex_agent.core.provider import StreamingLLMProvider
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.types import LLMResponse, ToolCall
from modex_agent.interceptor.abc import InterceptorScope, LLMStreamChunk
from modex_agent.memory.history import ListMessageHistory
from modex_agent.runtime.enums import AgentKind, TurnCustomKey, TurnPhase
from modex_agent.runtime.models import TurnIdentity
from modex_agent.runtime.services import AgentRuntime, AgentRuntimeServices


def _make_ctx():
    from modex_agent.core.agent import AgentContext
    from modex_agent.core.tool_manager import InMemoryToolManager

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
        tool_manager=InMemoryToolManager(),
        session=SessionInfo.from_str("test.agent"),
        max_iterations=5,
        identity=state.identity,
        runtime=runtime,
    )


class _PassthroughInterceptorChain:
    """Pass-through chain: iterates the wrapped stream without draining.

    The cancel is simulated by the fake provider raising mid-stream (equivalent
    to the real control-channel drain raising inside _on_content_delta).
    has_scope(LLM_STREAM)=True so ReactLlmClient.call() routes here (the
    original tests called _stream_with_control directly; routing through call()
    requires the scope gate).
    """

    def has_scope(self, scope: InterceptorScope) -> bool:
        return scope == InterceptorScope.LLM_STREAM

    async def around_llm_stream(self, ctx, call, next_stream) -> AsyncIterator[LLMStreamChunk]:
        async for chunk in next_stream():
            yield chunk


class _CancelBeforeYieldChain:
    """Simulates the real LlmCancelInterceptor post-stream cancel.

    The provider streams fully via callbacks (populating ``streamed_content``)
    and returns; the interceptor then drains and raises *before* re-yielding
    the end-of-stream chunk — so the for-loop never fills ``accumulated_content``
    yet ``streamed_content`` holds the content.
    """

    def __init__(self, exc: BaseException):
        self._exc = exc

    def has_scope(self, scope: InterceptorScope) -> bool:
        return scope == InterceptorScope.LLM_STREAM

    async def around_llm_stream(self, ctx, call, next_stream) -> AsyncIterator[LLMStreamChunk]:
        async for _chunk in next_stream():
            raise self._exc
        yield  # pragma: no cover - keeps this an async generator


class _FakeStreamProvider(StreamingLLMProvider):
    """Streams content via on_content_delta callbacks (the real path).

    Mirrors how a real provider streams: content deltas flow through the
    ``on_content_delta`` callback (which feeds ``streamed_content``), NOT through
    the end-of-stream chunk. Either raises mid-stream (``exc``) or returns
    ``response`` after streaming completes.
    """

    def __init__(
        self,
        deltas: list[str],
        *,
        exc: BaseException | None = None,
        response: LLMResponse | None = None,
    ):
        self._deltas = deltas
        self._exc = exc
        self._response = response

    def get_default_model(self) -> str:
        return "mock"

    async def chat(self, messages, **kw):  # pragma: no cover - unused in streaming path
        raise RuntimeError("not used")

    async def chat_stream(self, messages, *, on_content_delta, on_reasoning_delta, **kw):
        for delta in self._deltas:
            await on_content_delta(delta)
        if self._exc is not None:
            raise self._exc
        return self._response


class _FakeEmitter:
    def wants_streaming(self) -> bool:
        return True

    async def emit(self, event, data=None):
        pass

    async def emit_delta(self, delta: str):
        pass

    async def emit_stream_end(self, resuming: bool = False):
        pass


class _FakeNonStreamEmitter(_FakeEmitter):
    def wants_streaming(self) -> bool:
        return False

    async def emit_content(self, content: str):
        pass


class _FakeNonStreamProvider:
    def __init__(self, *, response: LLMResponse):
        self._response = response

    def get_default_model(self) -> str:
        return "mock"

    async def chat(self, messages, **kw):
        return self._response


class TestReactLlmClientStreamCaptureStashesPartial:
    """ReactLlmClient.call's control-drain path must stash the live partial.

    call() routes here because the ctx wires an LLM_STREAM-scope interceptor
    chain; on mid-stream interrupt the except-block writes
    ``state.custom[TurnCustomKey.INTERRUPTED_PARTIAL]``. The agent's run()
    cancel/error handler then persists it (see test_react_agent_interrupted_partial
    TestPersistInterruptedPartial, kept at the agent level).
    """

    @pytest.mark.asyncio
    async def test_streamed_content_stashed_on_midstream_cancel(self):
        """Real flow: content streams via on_content_delta, cancel raises
        mid-stream before chat_stream returns. streamed_content (not
        accumulated_content) holds the partial and must be stashed."""
        ctx = _make_ctx()
        ctx.emitter = _FakeEmitter()
        provider = _FakeStreamProvider(["partial ", "content"], exc=AgentCancelledError())
        ctx.runtime.services.interceptors = _PassthroughInterceptorChain()

        with pytest.raises(AgentCancelledError):
            await ReactLlmClient(provider).call([], ctx)

        partial = ctx.runtime.state.custom.get(TurnCustomKey.INTERRUPTED_PARTIAL)
        assert partial == {"content": "partial content", "tool_names": []}

    @pytest.mark.asyncio
    async def test_streamed_content_and_tools_stashed_on_poststream_cancel(self):
        """Provider streams fully and returns; the interceptor cancels before
        re-yielding the chunk (real LlmCancelInterceptor post-stream drain).
        streamed_content holds the content, tool_calls_list holds tool names."""
        ctx = _make_ctx()
        ctx.emitter = _FakeEmitter()
        provider = _FakeStreamProvider(
            ["full ", "content"],
            response=LLMResponse(
                content="full content",
                tool_calls=[ToolCall(tool_name="read_file", arguments={}, call_id="c1")],
            ),
        )
        ctx.runtime.services.interceptors = _CancelBeforeYieldChain(AgentCancelledError())

        with pytest.raises(AgentCancelledError):
            await ReactLlmClient(provider).call([], ctx)

        partial = ctx.runtime.state.custom.get(TurnCustomKey.INTERRUPTED_PARTIAL)
        assert partial == {"content": "full content", "tool_names": ["read_file"]}

    @pytest.mark.asyncio
    async def test_no_stash_when_nothing_produced(self):
        ctx = _make_ctx()
        ctx.emitter = _FakeEmitter()

        class _EmptyRaising:
            def has_scope(self, scope: InterceptorScope) -> bool:
                return scope == InterceptorScope.LLM_STREAM

            async def around_llm_stream(self, ctx, call, next_stream):
                raise AgentCancelledError()
                yield  # pragma: no cover - makes this an async generator

        ctx.runtime.services.interceptors = _EmptyRaising()

        with pytest.raises(AgentCancelledError):
            await ReactLlmClient(MagicMock(spec=StreamingLLMProvider)).call([], ctx)

        assert TurnCustomKey.INTERRUPTED_PARTIAL not in ctx.runtime.state.custom


class TestStreamWithControlPreservesUsage:
    """_stream_with_control must propagate usage from the provider response.

    Regression: the control-drain path reconstructed LLMResponse with only
    content/reasoning/finish_reason/tool_calls — dropping ``response.usage``,
    so the trace hook never saw token counts.
    """

    async def test_usage_propagated_through_control_drain_path(self):
        ctx = _make_ctx()
        ctx.emitter = _FakeEmitter()
        provider = _FakeStreamProvider(
            ["hello"],
            response=LLMResponse(
                content="hello",
                usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            ),
        )
        ctx.runtime.services.interceptors = _PassthroughInterceptorChain()

        result = await ReactLlmClient(provider).call([], ctx)

        assert result.usage == {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}

    async def test_usage_propagated_through_plain_stream_path(self):
        ctx = _make_ctx()
        ctx.emitter = _FakeEmitter()
        provider = _FakeStreamProvider(
            ["hello"],
            response=LLMResponse(
                content="hello",
                usage={"prompt_tokens": 20, "completion_tokens": 8, "total_tokens": 28},
            ),
        )
        # No interceptor chain → routes through _stream_with_recovery → _stream_plain
        ctx.runtime.services.interceptors = None

        result = await ReactLlmClient(provider).call([], ctx)

        assert result.usage == {"prompt_tokens": 20, "completion_tokens": 8, "total_tokens": 28}

    async def test_usage_propagated_through_non_streaming_path(self):
        ctx = _make_ctx()
        # Non-streaming: emitter.wants_streaming() must be False
        ctx.emitter = _FakeNonStreamEmitter()
        provider = _FakeNonStreamProvider(
            response=LLMResponse(
                content="hello",
                usage={"prompt_tokens": 30, "completion_tokens": 12, "total_tokens": 42},
            ),
        )

        result = await ReactLlmClient(provider).call([], ctx)

        assert result.usage == {"prompt_tokens": 30, "completion_tokens": 12, "total_tokens": 42}


class TestCompletionStartTimePropagation:
    """completion_start_time (TTFT) must flow from provider → LLMResponse → hook.

    Langfuse maps ``langfuse.observation.completion_start_time`` to its
    ``completionStartTime`` field — the only direct TTFT path. The provider
    captures it at first content delta; _stream_with_control must not drop it.
    """

    async def test_completion_start_time_through_control_drain_path(self):
        ctx = _make_ctx()
        ctx.emitter = _FakeEmitter()
        provider = _FakeStreamProvider(
            ["hello"],
            response=LLMResponse(
                content="hello",
                completion_start_time="2025-01-01T00:00:00.123456+00:00",
            ),
        )
        ctx.runtime.services.interceptors = _PassthroughInterceptorChain()

        result = await ReactLlmClient(provider).call([], ctx)

        assert result.completion_start_time == "2025-01-01T00:00:00.123456+00:00"

    async def test_completion_start_time_through_plain_stream_path(self):
        ctx = _make_ctx()
        ctx.emitter = _FakeEmitter()
        provider = _FakeStreamProvider(
            ["hello"],
            response=LLMResponse(
                content="hello",
                completion_start_time="2025-01-01T00:00:00.654321+00:00",
            ),
        )
        ctx.runtime.services.interceptors = None

        result = await ReactLlmClient(provider).call([], ctx)

        assert result.completion_start_time == "2025-01-01T00:00:00.654321+00:00"

    async def test_completion_start_time_none_when_not_provided(self):
        ctx = _make_ctx()
        ctx.emitter = _FakeEmitter()
        provider = _FakeStreamProvider(
            ["hello"],
            response=LLMResponse(content="hello"),
        )
        ctx.runtime.services.interceptors = _PassthroughInterceptorChain()

        result = await ReactLlmClient(provider).call([], ctx)

        assert result.completion_start_time is None


class _RecordingProvider(StreamingLLMProvider):
    """Records the temperature kwarg received on each call path."""

    def __init__(self):
        self.stream_temperatures: list[float | None] = []
        self.chat_temperatures: list[float | None] = []

    def get_default_model(self) -> str:
        return "mock"

    async def chat(self, messages, **kw):
        self.chat_temperatures.append(kw.get("temperature"))
        return LLMResponse(content="ok")

    async def chat_stream(self, messages, **kw):
        self.stream_temperatures.append(kw.get("temperature"))
        return LLMResponse(content="ok")


class TestTemperaturePassThrough:
    """ctx.temperature must reach the provider verbatim.

    Regression: the call sites sent ``ctx.temperature or 0.7`` — with
    ctx.temperature None in practice, every ReAct call hardcoded 0.7 and the
    provider-constructor value (model.yml temperature) never reached the API.
    None must now flow through so the provider falls back to its ctor value;
    a per-turn override still wins.
    """

    async def test_plain_stream_path_passes_none(self):
        ctx = _make_ctx()
        ctx.emitter = _FakeEmitter()
        ctx.runtime.services.interceptors = None
        provider = _RecordingProvider()

        await ReactLlmClient(provider).call([], ctx)

        assert provider.stream_temperatures == [None]

    async def test_control_drain_path_passes_none(self):
        ctx = _make_ctx()
        ctx.emitter = _FakeEmitter()
        ctx.runtime.services.interceptors = _PassthroughInterceptorChain()
        provider = _RecordingProvider()

        await ReactLlmClient(provider).call([], ctx)

        assert provider.stream_temperatures == [None]

    async def test_non_streaming_path_passes_none(self):
        ctx = _make_ctx()
        ctx.emitter = _FakeNonStreamEmitter()
        provider = _RecordingProvider()

        await ReactLlmClient(provider).call([], ctx)

        assert provider.chat_temperatures == [None]

    async def test_per_turn_override_passes_through(self):
        ctx = _make_ctx()
        ctx.emitter = _FakeEmitter()
        ctx.runtime.services.interceptors = None
        ctx.temperature = 0.3
        provider = _RecordingProvider()

        await ReactLlmClient(provider).call([], ctx)

        assert provider.stream_temperatures == [0.3]
