"""Tests for WebBotEmitter streaming event emitter and CompositeEmitter."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from bot.adapters.web_socket import WebSocketInputAdapter, WebSocketOutputAdapter
from bot.webui.emitter import CompositeEmitter, WebBotEmitter
from bot.webui.events import (
    ToolResultEvent,
    WebUIEventType,
)
from bot.webui.transcript_store import JSONLTranscriptStore

from modex_agent.agents.react.agent import ReActEvent
from modex_agent.agents.react.constants import ToolCallEndPayload
from modex_agent.core.emitter import AgentResult, ContentEmitter
from modex_agent.core.events import EmitterConfig
from modex_agent.core.tool_manager import ToolResult
from modex_agent.core.types import ToolCall


@pytest.mark.asyncio
async def test_emit_content_delta() -> None:
    input_adapter = WebSocketInputAdapter()
    output_adapter = WebSocketOutputAdapter(input_adapter)
    emitter = WebBotEmitter(output_adapter, "web:abc.main", config=EmitterConfig())
    input_adapter.register_connection("web:abc.main", None)
    await emitter.emit_delta("hello")
    q = input_adapter.get_delta_queue("web:abc.main", None)
    assert q is not None
    envelope = q.get_nowait()
    assert envelope.event_type == WebUIEventType.MODEL_CONTENT_DELTA.value
    assert envelope.payload["text"] == "hello"
    assert isinstance(envelope.payload["turn_id"], str)
    assert len(envelope.payload["turn_id"]) > 0
    assert envelope.session_id == "web:abc.main"
    assert envelope.agent_name == "main"


@pytest.mark.asyncio
async def test_emit_complete_sends_turn_end() -> None:
    input_adapter = WebSocketInputAdapter()
    output_adapter = WebSocketOutputAdapter(input_adapter)
    emitter = WebBotEmitter(output_adapter, "web:abc.main", config=EmitterConfig())
    input_adapter.register_connection("web:abc.main", None)
    await emitter.emit_complete(AgentResult(content="done"))
    q = input_adapter.get_delta_queue("web:abc.main", None)
    assert q is not None
    envelope = q.get_nowait()
    assert envelope.event_type == WebUIEventType.TURN_END.value


@pytest.mark.asyncio
async def test_streaming_does_not_save_deltas() -> None:
    """emit_delta pushes WS events but does NOT persist content to transcript."""
    with tempfile.TemporaryDirectory() as tmp:
        input_adapter = WebSocketInputAdapter()
        output_adapter = WebSocketOutputAdapter(input_adapter)
        store = JSONLTranscriptStore(Path(tmp))
        emitter = WebBotEmitter(
            output_adapter, "conv1.main",
            config=EmitterConfig(),
            transcript_store=store,
        )
        input_adapter.register_connection("conv1.main", None)

        await emitter.emit_delta("hello")
        await emitter.emit_delta(" world")

        events = await store.load("conv1.main")
        assert all(e.event != WebUIEventType.MODEL_CONTENT_DELTA.value for e in events)
        assert all(e.event != WebUIEventType.ASSISTANT_TEXT.value for e in events)

        q = input_adapter.get_delta_queue("conv1.main", None)
        assert q is not None
        assert q.qsize() == 2


@pytest.mark.asyncio
async def test_subagent_emitter_preserves_full_session_id() -> None:
    """Regression: a subagent session id carries an invocation_id segment.

    The emitter must keep the FULL session id (with invocation_id) in every
    event it emits AND persist the transcript keyed by that full id — so two
    reviewer invocations do not collapse into one transcript.
    """
    with tempfile.TemporaryDirectory() as tmp:
        input_adapter = WebSocketInputAdapter()
        output_adapter = WebSocketOutputAdapter(input_adapter)
        store = JSONLTranscriptStore(Path(tmp))
        full_sid = "conv1.reviewer.aa11bb22"
        emitter = WebBotEmitter(
            output_adapter, full_sid,
            config=EmitterConfig(),
            transcript_store=store,
        )
        input_adapter.register_connection(full_sid, None)

        await emitter.emit_content("review done")
        await emitter.emit_complete(AgentResult(content="review done"))

        # WebSocket delta events carry the FULL session id + correct agent.
        q = input_adapter.get_delta_queue(full_sid, None)
        assert q is not None
        envelope = q.get_nowait()
        assert envelope.event_type == WebUIEventType.TURN_END.value
        assert envelope.session_id == full_sid
        assert envelope.agent_name == "reviewer"

        # Transcript persisted under the FULL session id (not truncated).
        assert await store.load(full_sid)
        assert not await store.load("conv1.reviewer")


@pytest.mark.asyncio
async def test_two_subagent_emitters_persist_to_separate_transcripts() -> None:
    """Two reviewer invocations with different invocation_ids stay separate."""
    with tempfile.TemporaryDirectory() as tmp:
        input_adapter = WebSocketInputAdapter()
        output_adapter = WebSocketOutputAdapter(input_adapter)
        store = JSONLTranscriptStore(Path(tmp))

        for sid in ("conv1.reviewer.aa11", "conv1.reviewer.bb22"):
            em = WebBotEmitter(
                output_adapter, sid,
                config=EmitterConfig(),
                transcript_store=store,
            )
            await em.emit_content(sid)
            await em.emit_complete(AgentResult(content=sid))

        assert len(await store.load("conv1.reviewer.aa11")) >= 1
        assert len(await store.load("conv1.reviewer.bb22")) >= 1
        assert await store.list_sessions() == {
            "conv1.reviewer.aa11",
            "conv1.reviewer.bb22",
        }


# ── Incremental persistence tests ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_emit_content_saves_assistant_text_to_transcript() -> None:
    """Buffered text is flushed to the transcript store at stream/turn end."""
    with tempfile.TemporaryDirectory() as tmp:
        input_adapter = WebSocketInputAdapter()
        output_adapter = WebSocketOutputAdapter(input_adapter)
        store = JSONLTranscriptStore(Path(tmp))
        emitter = WebBotEmitter(output_adapter, "conv1.main", config=EmitterConfig(), transcript_store=store)
        input_adapter.register_connection("conv1.main", None)
        await emitter.emit_content("Hello World")
        await emitter.emit_stream_end(resuming=False)
        events = await store.load("conv1.main")
        # TurnStartEvent is WebSocket-only (not persisted). Only AssistantTextEvent.
        assert len(events) == 1
        assert events[0].event == WebUIEventType.ASSISTANT_TEXT.value


@pytest.mark.asyncio
async def test_emit_complete_flushes_remaining_text_buffer() -> None:
    """If emit_stream_end is not called, emit_complete flushes the buffer."""
    with tempfile.TemporaryDirectory() as tmp:
        input_adapter = WebSocketInputAdapter()
        output_adapter = WebSocketOutputAdapter(input_adapter)
        store = JSONLTranscriptStore(Path(tmp))
        emitter = WebBotEmitter(output_adapter, "conv1.main", config=EmitterConfig(), transcript_store=store)
        input_adapter.register_connection("conv1.main", None)
        await emitter.emit_content("Hello World")
        await emitter.emit_complete(AgentResult(content="done"))
        events = await store.load("conv1.main")
        assert any(e.event == WebUIEventType.ASSISTANT_TEXT.value for e in events)
        assert not any(e.event == WebUIEventType.TURN_END.value for e in events)


@pytest.mark.asyncio
async def test_tool_call_events_persisted_incrementally() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        input_adapter = WebSocketInputAdapter()
        output_adapter = WebSocketOutputAdapter(input_adapter)
        store = JSONLTranscriptStore(Path(tmp))
        emitter = WebBotEmitter(output_adapter, "conv1.main", config=EmitterConfig(), transcript_store=store)
        input_adapter.register_connection("conv1.main", None)
        tc = ToolCall(tool_name="read_file", arguments={"path": "/x"}, call_id="call_0")
        result = ToolResult.from_text("read_file", "content")
        await emitter.emit(ReActEvent.TOOL_CALL_START, tc)
        await emitter.emit(
            ReActEvent.TOOL_CALL_END,
            ToolCallEndPayload(tool_call=tc, result=result, seq=7),
        )
        events = await store.load("conv1.main")
        assert any(e.event == WebUIEventType.TOOL_CALL.value for e in events)
        assert any(e.event == WebUIEventType.TOOL_RESULT.value for e in events)
        tool_result = next(e for e in events if isinstance(e, ToolResultEvent))
        assert tool_result.seq == 7


@pytest.mark.asyncio
async def test_tool_call_events_stream_matching_call_id() -> None:
    """Streamed tool_call_start/end carry the SAME call_id.

    The frontend pairs a result with exactly one tool block by call_id —
    matching by tool name breaks when a turn runs parallel same-name calls.
    """
    input_adapter = WebSocketInputAdapter()
    output_adapter = WebSocketOutputAdapter(input_adapter)
    emitter = WebBotEmitter(output_adapter, "conv1.main", config=EmitterConfig())
    input_adapter.register_connection("conv1.main", None)
    tc = ToolCall(tool_name="read_file", arguments={"path": "/x"}, call_id="call_0")
    result = ToolResult.from_text("read_file", "content")
    await emitter.emit(ReActEvent.TOOL_CALL_START, tc)
    await emitter.emit(
        ReActEvent.TOOL_CALL_END,
        ToolCallEndPayload(tool_call=tc, result=result, seq=7),
    )
    q = input_adapter.get_delta_queue("conv1.main", None)
    assert q is not None
    start_env = q.get_nowait()
    end_env = q.get_nowait()
    assert start_env.event_type == WebUIEventType.TOOL_CALL_START.value
    assert start_env.payload["call_id"] == "call_0"
    assert end_env.event_type == WebUIEventType.TOOL_CALL_END.value
    assert end_env.payload["call_id"] == "call_0"
    assert end_env.payload["seq"] == 7


@pytest.mark.asyncio
async def test_tool_call_end_without_call_id_omits_wire_field() -> None:
    input_adapter = WebSocketInputAdapter()
    output_adapter = WebSocketOutputAdapter(input_adapter)
    emitter = WebBotEmitter(output_adapter, "conv1.main", config=EmitterConfig())
    input_adapter.register_connection("conv1.main", None)
    tool_call = ToolCall(tool_name="read_file", arguments={})

    await emitter.emit(
        ReActEvent.TOOL_CALL_END,
        ToolCallEndPayload(
            tool_call=tool_call,
            result=ToolResult.from_text("read_file", "content"),
            seq=0,
        ),
    )

    queue = input_adapter.get_delta_queue("conv1.main", None)
    assert queue is not None
    envelope = queue.get_nowait()
    assert "call_id" not in envelope.payload


@pytest.mark.asyncio
async def test_reasoning_not_persisted_to_transcript() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        input_adapter = WebSocketInputAdapter()
        output_adapter = WebSocketOutputAdapter(input_adapter)
        store = JSONLTranscriptStore(Path(tmp))
        emitter = WebBotEmitter(output_adapter, "conv1.main", config=EmitterConfig(), transcript_store=store)
        input_adapter.register_connection("conv1.main", None)
        await emitter.emit(ReActEvent.MODEL_REASONING, "thinking...")
        await emitter.emit_complete(AgentResult(content="done"))
        events = await store.load("conv1.main")
        assert not any(e.event == WebUIEventType.MODEL_REASONING_DELTA.value for e in events)


@pytest.mark.asyncio
async def test_emit_content_empty_skips_persist() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        input_adapter = WebSocketInputAdapter()
        output_adapter = WebSocketOutputAdapter(input_adapter)
        store = JSONLTranscriptStore(Path(tmp))
        emitter = WebBotEmitter(output_adapter, "conv1.main", config=EmitterConfig(), transcript_store=store)
        input_adapter.register_connection("conv1.main", None)
        await emitter.emit_content("   ")
        events = await store.load("conv1.main")
        assert all(e.event != WebUIEventType.ASSISTANT_TEXT.value for e in events)


@pytest.mark.asyncio
async def test_streaming_delta_flush_persists_content() -> None:
    """Regression: the control-interceptor stream path calls emit_delta +
    emit_stream_end, not emit_content.  Assistant text must still reach the
    transcript store.
    """
    with tempfile.TemporaryDirectory() as tmp:
        input_adapter = WebSocketInputAdapter()
        output_adapter = WebSocketOutputAdapter(input_adapter)
        store = JSONLTranscriptStore(Path(tmp))
        emitter = WebBotEmitter(
            output_adapter, "conv1.main", config=EmitterConfig(), transcript_store=store
        )
        input_adapter.register_connection("conv1.main", None)
        await emitter.emit_delta("Hello ")
        await emitter.emit_delta("world")
        await emitter.emit_stream_end(resuming=False)
        events = await store.load("conv1.main")
        assert any(e.event == WebUIEventType.ASSISTANT_TEXT.value for e in events), (
            f"Expected assistant text in transcript, got: {[e.event for e in events]}"
        )


# ── CompositeEmitter tests ────────────────────────────────────────────────


class _StubEmitter(ContentEmitter[ReActEvent]):
    """Recording emitter that tracks which methods were called."""

    def __init__(self) -> None:
        super().__init__(EmitterConfig())
        self.calls: list[str] = []

    async def emit_delta(self, delta: str) -> None:
        self.calls.append(f"delta:{delta}")

    async def emit_complete(self, result: AgentResult) -> None:
        self.calls.append(f"complete:{result.content}")

    async def emit_error(self, error: str) -> None:
        self.calls.append(f"error:{error}")

    def wants_streaming(self) -> bool:
        return True


class _FailingEmitter(ContentEmitter[ReActEvent]):
    """Emitter that raises on every method."""

    async def emit_delta(self, delta: str) -> None:
        raise RuntimeError("boom")

    async def emit_complete(self, result: AgentResult) -> None:
        raise RuntimeError("boom")

    async def emit_error(self, error: str) -> None:
        raise RuntimeError("boom")


@pytest.mark.asyncio
async def test_composite_fans_out_to_all_children() -> None:
    """CompositeEmitter delegates to all children."""
    stub1 = _StubEmitter()
    stub2 = _StubEmitter()
    composite = CompositeEmitter[ReActEvent](emitters=[stub1, stub2])

    await composite.emit_delta("hello")
    await composite.emit_complete(AgentResult(content="done"))

    assert stub1.calls == ["delta:hello", "complete:done"]
    assert stub2.calls == ["delta:hello", "complete:done"]


@pytest.mark.asyncio
async def test_composite_error_isolation() -> None:
    """One failing child does not prevent others from receiving events."""
    stub = _StubEmitter()
    failing = _FailingEmitter()
    composite = CompositeEmitter[ReActEvent](emitters=[failing, stub])

    await composite.emit_delta("test")
    assert stub.calls == ["delta:test"]


@pytest.mark.asyncio
async def test_composite_wants_streaming_or_semantics() -> None:
    """wants_streaming returns True if ANY child wants streaming."""
    no_stream = _StubEmitter()
    no_stream.calls = []  # reset

    class _NoStreaming(ContentEmitter[ReActEvent]):
        async def emit_delta(self, delta: str) -> None:
            pass
        async def emit_complete(self, result: AgentResult) -> None:
            pass
        async def emit_error(self, error: str) -> None:
            pass

    composite = CompositeEmitter[ReActEvent](
        emitters=[_NoStreaming(), _StubEmitter()],
    )
    assert composite.wants_streaming() is True

    composite2 = CompositeEmitter[ReActEvent](
        emitters=[_NoStreaming(), _NoStreaming()],
    )
    assert composite2.wants_streaming() is False
