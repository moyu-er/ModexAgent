"""Tests for StreamingAwareEmitter (modex_agent.adapters.emitter).

Verifies the full NATIVE / PSEUDO / NONE streaming-mode matrix:
- NATIVE: deltas forwarded immediately via send_delta
- PSEUDO: deltas buffered, flushed on stream_end / complete
- NONE: deltas buffered, wants_streaming False

Plus the B4 gap cases: per-session isolation, exactly-once flush,
attachment projection, and the _safe_adapter_send timeout path.
"""

import asyncio

from modex_agent.adapters.emitter import StreamingAwareEmitter
from modex_agent.adapters.output import OutputAdapter
from modex_agent.adapters.platform import StreamingMode
from modex_agent.agents.react import ReActEvent, ToolCallEndPayload
from modex_agent.core.emitter import AgentResult
from modex_agent.core.tool_manager import ToolResult
from modex_agent.core.turn_events import (
    TurnReasoningEvent,
    TurnTextEvent,
    TurnToolCallEvent,
    TurnToolResultEvent,
)
from modex_agent.core.types import OutputMessage, ToolCall


class RecordingOutputAdapter(OutputAdapter):
    """Recording OutputAdapter subclass (the real ABC, per plan §18.4)."""

    def __init__(self, mode: StreamingMode = StreamingMode.NATIVE) -> None:
        self._mode = mode
        self.sends: list[tuple[OutputMessage, str]] = []
        self.send_deltas: list[tuple[str, str]] = []

    @property
    def name(self) -> str:
        return "recording"

    @property
    def streaming_mode(self) -> StreamingMode:
        return self._mode

    async def send(self, message: OutputMessage, session_id: str) -> None:
        self.sends.append((message, session_id))

    async def send_delta(
        self, delta: str, session_id: str, metadata: dict | None = None
    ) -> None:
        self.send_deltas.append((delta, session_id))


class SlowOutputAdapter(RecordingOutputAdapter):
    """send() never completes — drives the _safe_adapter_send timeout path."""

    async def send(self, message: OutputMessage, session_id: str) -> None:
        await asyncio.Event().wait()


def _emitter(adapter: RecordingOutputAdapter, session: str = "test_session"):
    return StreamingAwareEmitter(output_adapter=adapter, session_id=session)


class TestStreamingModeMatrix:
    """Full NATIVE / PSEUDO / NONE behavior matrix."""

    def test_native_is_true_streaming(self):
        emitter = _emitter(RecordingOutputAdapter(StreamingMode.NATIVE))
        assert emitter.is_true_streaming is True
        assert emitter.wants_streaming() is True

    def test_pseudo_buffers_but_wants_streaming(self):
        adapter = RecordingOutputAdapter(StreamingMode.PSEUDO)
        emitter = _emitter(adapter)
        assert emitter.is_true_streaming is False
        assert emitter.wants_streaming() is True

    def test_none_no_streaming(self):
        adapter = RecordingOutputAdapter(StreamingMode.NONE)
        emitter = _emitter(adapter)
        assert emitter.is_true_streaming is False
        assert emitter.wants_streaming() is False

    async def test_native_forwards_deltas_immediately(self):
        adapter = RecordingOutputAdapter(StreamingMode.NATIVE)
        emitter = _emitter(adapter)
        await emitter.emit_delta("Hello ")
        await emitter.emit_delta("World")
        assert adapter.send_deltas == [("Hello ", "test_session"), ("World", "test_session")]
        assert adapter.sends == []

    async def test_pseudo_buffers_until_flush(self):
        adapter = RecordingOutputAdapter(StreamingMode.PSEUDO)
        emitter = _emitter(adapter)
        await emitter.emit_delta("Hello ")
        await emitter.emit_delta("World")
        assert adapter.send_deltas == []
        assert emitter._content_buffer == "Hello World"
        await emitter.emit_stream_end(resuming=True)
        assert len(adapter.sends) == 1
        assert adapter.sends[0][0].content == "Hello World"
        assert emitter._content_buffer == ""

    async def test_none_buffers_until_complete(self):
        adapter = RecordingOutputAdapter(StreamingMode.NONE)
        emitter = _emitter(adapter)
        await emitter.emit_delta("Hi")
        await emitter.emit_stream_end(resuming=False)
        assert len(adapter.sends) == 1
        assert adapter.sends[0][0].content == "Hi"

    async def test_emit_delta_empty_ignored(self):
        adapter = RecordingOutputAdapter(StreamingMode.NATIVE)
        emitter = _emitter(adapter)
        await emitter.emit_delta("")
        assert adapter.send_deltas == []
        assert emitter._content_buffer == ""

    async def test_emit_content_buffers_full_content(self):
        adapter = RecordingOutputAdapter(StreamingMode.PSEUDO)
        emitter = _emitter(adapter)
        await emitter.emit_content("full text")
        assert emitter._content_buffer == "full text"


class TestEventHandling:
    async def test_model_reasoning_buffers(self):
        adapter = RecordingOutputAdapter(StreamingMode.PSEUDO)
        emitter = _emitter(adapter)
        await emitter.emit(ReActEvent.MODEL_REASONING, "Let me think... ")
        await emitter.emit(ReActEvent.MODEL_REASONING, "About this...")
        assert emitter._reasoning_buffer == "Let me think... About this..."
        assert adapter.sends == []

    async def test_final_output_flushes_in_pseudo_mode(self):
        adapter = RecordingOutputAdapter(StreamingMode.PSEUDO)
        emitter = _emitter(adapter)
        await emitter.emit_delta("answer")
        await emitter.emit(ReActEvent.FINAL_OUTPUT, None)
        assert len(adapter.sends) == 1
        assert adapter.sends[0][0].content == "answer"

    async def test_error_event_sends_error_message(self):
        adapter = RecordingOutputAdapter(StreamingMode.PSEUDO)
        emitter = _emitter(adapter)
        await emitter.emit(ReActEvent.ERROR, "boom")
        assert len(adapter.sends) == 1
        assert "Error: boom" in adapter.sends[0][0].content

    async def test_tool_events_do_not_touch_adapter(self):
        adapter = RecordingOutputAdapter(StreamingMode.NATIVE)
        emitter = _emitter(adapter)
        tool_call = ToolCall(tool_name="test_tool", arguments={"arg": "value"})
        await emitter.emit(ReActEvent.TOOL_CALL_START, tool_call)
        result = ToolResult.from_text("test_tool", "success")
        await emitter.emit(
            ReActEvent.TOOL_CALL_END,
            ToolCallEndPayload(tool_call=tool_call, result=result, seq=0),
        )
        assert adapter.send_deltas == []
        assert adapter.sends == []

    async def test_turn_event_forwards_only_canonical_text(self):
        adapter = RecordingOutputAdapter(StreamingMode.NATIVE)
        emitter = _emitter(adapter)
        await emitter.emit_turn_event(TurnTextEvent(text="Hello"))
        await emitter.emit_turn_event(TurnReasoningEvent(text="thinking"))
        await emitter.emit_turn_event(
            TurnToolCallEvent(tool_name="bash", call_id="call-1", arguments={"command": "ls"})
        )
        await emitter.emit_turn_event(
            TurnToolResultEvent(tool_name="bash", call_id="call-1", output="file.txt")
        )
        assert adapter.send_deltas == [("Hello", "test_session")]

    async def test_emit_stream_end_repeated_no_duplicate_send(self):
        """Second stream_end after a flush must not re-send empty content."""
        adapter = RecordingOutputAdapter(StreamingMode.PSEUDO)
        emitter = _emitter(adapter)
        await emitter.emit_delta("First iteration")
        await emitter.emit_stream_end(resuming=True)
        await emitter.emit_stream_end(resuming=True)
        assert len(adapter.sends) == 1
        assert adapter.sends[0][0].content == "First iteration"

    async def test_second_iteration_does_not_accumulate(self):
        adapter = RecordingOutputAdapter(StreamingMode.PSEUDO)
        emitter = _emitter(adapter)
        await emitter.emit_delta("First iteration")
        await emitter.emit_stream_end(resuming=True)
        await emitter.emit_delta("Second iteration")
        await emitter.emit_stream_end(resuming=True)
        assert len(adapter.sends) == 2
        assert adapter.sends[1][0].content == "Second iteration"
        assert emitter._content_buffer == ""


class TestEmitComplete:
    async def test_complete_flushes_pseudo_with_reasoning_metadata(self):
        adapter = RecordingOutputAdapter(StreamingMode.PSEUDO)
        emitter = _emitter(adapter)
        await emitter.emit_delta("Hello World")
        await emitter.emit(ReActEvent.MODEL_REASONING, "Some reasoning")
        await emitter.emit_complete(
            AgentResult(content="Hello World", reasoning="Some reasoning")
        )
        assert len(adapter.sends) == 1
        message, session_id = adapter.sends[0]
        assert message.content == "Hello World"
        assert message.metadata.get("reasoning") == "Some reasoning"
        assert session_id == "test_session"
        assert emitter._content_buffer == ""
        assert emitter._reasoning_buffer == ""

    async def test_complete_native_clears_buffers_without_send(self):
        adapter = RecordingOutputAdapter(StreamingMode.NATIVE)
        emitter = _emitter(adapter)
        await emitter.emit_delta("Hello")
        await emitter.emit_complete(AgentResult(content="Hello"))
        assert adapter.sends == []
        assert emitter._content_buffer == ""

    async def test_emit_error_sends_via_adapter(self):
        adapter = RecordingOutputAdapter(StreamingMode.PSEUDO)
        emitter = _emitter(adapter)
        await emitter.emit_error("Something went wrong")
        assert len(adapter.sends) == 1
        assert "Error: Something went wrong" in adapter.sends[0][0].content
        assert adapter.sends[0][1] == "test_session"


class TestGapCases:
    """B4 gap coverage (plan §18.4)."""

    async def test_per_session_isolation(self):
        """Two emitters on one adapter never see each other's buffers."""
        adapter = RecordingOutputAdapter(StreamingMode.PSEUDO)
        emitter_a = _emitter(adapter, session="sess-a")
        emitter_b = _emitter(adapter, session="sess-b")
        await emitter_a.emit_delta("for A")
        await emitter_b.emit_delta("for B")
        await emitter_a.emit_complete(AgentResult(content="for A"))
        await emitter_b.emit_complete(AgentResult(content="for B"))
        a_sends = [(m.content, sid) for m, sid in adapter.sends if sid == "sess-a"]
        b_sends = [(m.content, sid) for m, sid in adapter.sends if sid == "sess-b"]
        assert a_sends == [("for A", "sess-a")]
        assert b_sends == [("for B", "sess-b")]

    async def test_exactly_once_flush_on_complete(self):
        """emit_complete must not double-send content already flushed by stream_end."""
        adapter = RecordingOutputAdapter(StreamingMode.PSEUDO)
        emitter = _emitter(adapter)
        await emitter.emit_delta("chunk1")
        await emitter.emit_stream_end(resuming=True)  # flush #1
        await emitter.emit_delta("chunk2")
        await emitter.emit_complete(AgentResult(content="chunk1chunk2"))  # flush #2 (chunk2 only)
        contents = [m.content for m, _ in adapter.sends]
        assert contents == ["chunk1", "chunk2"]

    async def test_attachment_projection(self):
        """emit_complete forwards result.attachments as an explicit OutputMessage."""
        adapter = RecordingOutputAdapter(StreamingMode.PSEUDO)
        emitter = _emitter(adapter)
        await emitter.emit_delta("done text")
        await emitter.emit_complete(
            AgentResult(content="done text", attachments=["a.png", "b.pdf"])
        )
        assert len(adapter.sends) == 2
        assert adapter.sends[0][0].content == "done text"
        attachment_message = adapter.sends[1][0]
        assert attachment_message.content == ""
        assert list(attachment_message.attachments) == ["a.png", "b.pdf"]

    async def test_attachments_sent_even_in_native_mode(self):
        adapter = RecordingOutputAdapter(StreamingMode.NATIVE)
        emitter = _emitter(adapter)
        await emitter.emit_complete(AgentResult(content="x", attachments=["f.txt"]))
        assert len(adapter.sends) == 1
        assert list(adapter.sends[0][0].attachments) == ["f.txt"]

    async def test_safe_adapter_send_timeout_path(self):
        """A hung adapter send is cut off by send_timeout instead of deadlocking."""
        adapter = SlowOutputAdapter(StreamingMode.PSEUDO)
        emitter = StreamingAwareEmitter(
            output_adapter=adapter,
            session_id="test_session",
            send_timeout=0.05,
        )
        await emitter.emit_delta("data")
        # Flush goes through _safe_adapter_send; timeout must return, not hang.
        await asyncio.wait_for(emitter.emit_stream_end(resuming=True), timeout=2.0)
        # Buffers are NOT cleared on timeout (flush failed) — but no exception raised.
