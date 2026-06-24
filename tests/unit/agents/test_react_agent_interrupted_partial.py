"""Tests for persisting partial assistant content when an LLM stream is interrupted.

Regression: on mid-stream cancel/pause, the assistant message append at the LLM
node (ctx.history.append) never ran, so memory lost the partial content while the
transcript kept it. The fix stashes the partial content in
``TurnCustomKey.INTERRUPTED_PARTIAL`` from ``_stream_with_control`` and persists
an XML-marked interrupted message in the agent's cancel/error handlers.
"""

import asyncio
from typing import AsyncIterator
from unittest.mock import MagicMock

import pytest

from framework.agents.react.agent import (
    ReActAgent,
    _interrupt_reason_from,
    _persist_interrupted_partial,
)
from framework.agents.react.state import ReActTurnState
from framework.control.exceptions import (
    AgentCancelled,
    AgentTimeout,
    PolicyViolation,
)
from framework.core.message import ContentFormat
from framework.core.provider import StreamingLLMProvider
from framework.core.session_id import SessionInfo
from framework.core.types import LLMResponse, ToolCall
from framework.interceptor.abc import LLMStreamChunk
from framework.memory.history import ListMessageHistory
from framework.runtime.enums import AgentKind, TurnCustomKey, TurnPhase
from framework.runtime.models import TurnIdentity
from framework.runtime.services import AgentRuntime, AgentRuntimeServices


def _make_ctx():
    from framework.core.agent import AgentContext
    from framework.core.tool_manager import InMemoryToolManager

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
    """

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


class TestInterruptReasonMapping:
    def test_user_stop_for_agent_cancelled(self):
        assert _interrupt_reason_from(AgentCancelled()) == "user_stop"

    def test_timeout_for_agent_timeout(self):
        assert _interrupt_reason_from(AgentTimeout()) == "timeout"

    def test_policy_for_policy_violation(self):
        assert _interrupt_reason_from(PolicyViolation()) == "policy"

    def test_cancelled_for_asyncio_cancelled(self):
        assert _interrupt_reason_from(asyncio.CancelledError()) == "cancelled"

    def test_error_for_generic_exception(self):
        assert _interrupt_reason_from(RuntimeError("boom")) == "error"


class TestStreamCaptureStashesPartial:
    @pytest.mark.asyncio
    async def test_streamed_content_stashed_on_midstream_cancel(self):
        """Real flow: content streams via on_content_delta, cancel raises
        mid-stream before chat_stream returns. streamed_content (not
        accumulated_content) holds the partial and must be stashed."""
        ctx = _make_ctx()
        ctx.emitter = _FakeEmitter()
        provider = _FakeStreamProvider(["partial ", "content"], exc=AgentCancelled())
        agent = ReActAgent(provider=provider)
        ctx.runtime.services.interceptors = _PassthroughInterceptorChain()

        with pytest.raises(AgentCancelled):
            await agent._stream_with_control([], ctx)

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
        agent = ReActAgent(provider=provider)
        ctx.runtime.services.interceptors = _CancelBeforeYieldChain(AgentCancelled())

        with pytest.raises(AgentCancelled):
            await agent._stream_with_control([], ctx)

        partial = ctx.runtime.state.custom.get(TurnCustomKey.INTERRUPTED_PARTIAL)
        assert partial == {"content": "full content", "tool_names": ["read_file"]}

    @pytest.mark.asyncio
    async def test_no_stash_when_nothing_produced(self):
        ctx = _make_ctx()

        class _EmptyRaising:
            async def around_llm_stream(self, ctx, call, next_stream):
                raise AgentCancelled()
                yield  # pragma: no cover - makes this an async generator

        ctx.runtime.services.interceptors = _EmptyRaising()
        agent = ReActAgent(provider=MagicMock(spec=StreamingLLMProvider))

        with pytest.raises(AgentCancelled):
            await agent._stream_with_control([], ctx)

        assert TurnCustomKey.INTERRUPTED_PARTIAL not in ctx.runtime.state.custom


class TestPersistInterruptedPartial:
    @pytest.mark.asyncio
    async def test_appends_xml_message_to_history_and_message_delta(self):
        ctx = _make_ctx()
        ctx.runtime.state.custom[TurnCustomKey.INTERRUPTED_PARTIAL] = {
            "content": "partial content",
            "tool_names": ["read_file"],
        }

        await _persist_interrupted_partial(ctx, "user_stop")

        history = await ctx.history.to_list()
        assert len(history) == 1
        msg = history[0]
        assert msg["role"] == "assistant"
        assert msg["content_format"] == ContentFormat.XML.value
        assert msg["truncatable_paths"] == ["content"]
        assert "<interrupted_response" in msg["content"]
        assert "partial content" in msg["content"]
        # Mirrored into message_delta so _get_turn_messages stays consistent.
        assert len(ctx.runtime.state.message_delta) == 1
        # Stash cleared after persist.
        assert TurnCustomKey.INTERRUPTED_PARTIAL not in ctx.runtime.state.custom

    @pytest.mark.asyncio
    async def test_noop_when_no_partial_stashed(self):
        ctx = _make_ctx()
        await _persist_interrupted_partial(ctx, "error")
        assert await ctx.history.to_list() == []
        assert ctx.runtime.state.message_delta == []

    @pytest.mark.asyncio
    async def test_noop_when_partial_empty(self):
        ctx = _make_ctx()
        ctx.runtime.state.custom[TurnCustomKey.INTERRUPTED_PARTIAL] = {
            "content": "",
            "tool_names": [],
        }
        await _persist_interrupted_partial(ctx, "error")
        assert await ctx.history.to_list() == []
