"""Tests for BufferingSink (modex_agent.adapters.emitter).

Verifies the full STREAMING / SEGMENT / TURN delivery-policy matrix
(mapped from the adapter NATIVE / PSEUDO / NONE streaming modes):
- STREAMING: text deltas forwarded immediately via send_delta
- SEGMENT: text buffered, flushed on iteration_finished / turn_finished
- TURN: text buffered, flushed on turn_finished only

Plus the carried-over gap cases: per-session isolation, exactly-once
flush, attachment projection, the mid-flight vs terminal error render,
and the _safe_adapter_send timeout path.
"""

import asyncio
import contextlib

from modex_agent.adapters.emitter import BufferingSink, DeliveryPolicy
from modex_agent.adapters.output import OutputAdapter
from modex_agent.adapters.platform import StreamingMode
from modex_agent.core.emitter import AgentResult, turn_finished_event
from modex_agent.core.turn_events import (
    IterationFinishedEvent,
    StopReason,
    TurnErroredEvent,
    TurnFinishedEvent,
    TurnReasoningEvent,
    TurnTextEvent,
    TurnToolCallEvent,
    TurnToolResultEvent,
)
from modex_agent.messaging.models import OutputMessage


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
    return BufferingSink(output_adapter=adapter, session_id=session)


def _iteration_end() -> IterationFinishedEvent:
    return IterationFinishedEvent(iteration=1, has_tool_calls=True)


class TestDeliveryPolicyMatrix:
    """Full STREAMING / SEGMENT / TURN behavior matrix."""

    def test_policy_maps_streaming_modes(self):
        assert DeliveryPolicy.of_streaming_mode(StreamingMode.NATIVE) is DeliveryPolicy.STREAMING
        assert DeliveryPolicy.of_streaming_mode(StreamingMode.PSEUDO) is DeliveryPolicy.SEGMENT
        assert DeliveryPolicy.of_streaming_mode(StreamingMode.NONE) is DeliveryPolicy.TURN
        # Unknown values fall back to the adapter-base PSEUDO default.
        assert DeliveryPolicy.of_streaming_mode("untyped") is DeliveryPolicy.SEGMENT

    def test_native_is_true_streaming(self):
        emitter = _emitter(RecordingOutputAdapter(StreamingMode.NATIVE))
        assert emitter.is_true_streaming is True
        assert emitter.wants_streaming() is True

    def test_pseudo_buffers_but_wants_streaming(self):
        emitter = _emitter(RecordingOutputAdapter(StreamingMode.PSEUDO))
        assert emitter.is_true_streaming is False
        assert emitter.wants_streaming() is True

    def test_none_no_streaming(self):
        emitter = _emitter(RecordingOutputAdapter(StreamingMode.NONE))
        assert emitter.is_true_streaming is False
        assert emitter.wants_streaming() is False

    async def test_streaming_forwards_deltas_immediately(self):
        adapter = RecordingOutputAdapter(StreamingMode.NATIVE)
        emitter = _emitter(adapter)
        await emitter.emit(TurnTextEvent(text="Hello "))
        await emitter.emit(TurnTextEvent(text="World"))
        assert adapter.send_deltas == [("Hello ", "test_session"), ("World", "test_session")]
        assert adapter.sends == []

    async def test_segment_buffers_until_iteration_end(self):
        adapter = RecordingOutputAdapter(StreamingMode.PSEUDO)
        emitter = _emitter(adapter)
        await emitter.emit(TurnTextEvent(text="Hello "))
        await emitter.emit(TurnTextEvent(text="World"))
        assert adapter.send_deltas == []
        assert emitter._content_buffer == "Hello World"
        await emitter.emit(_iteration_end())
        assert len(adapter.sends) == 1
        assert adapter.sends[0][0].content == "Hello World"
        assert emitter._content_buffer == ""

    async def test_turn_policy_buffers_until_turn_finished(self):
        adapter = RecordingOutputAdapter(StreamingMode.NONE)
        emitter = _emitter(adapter)
        await emitter.emit(TurnTextEvent(text="Hi"))
        await emitter.emit(_iteration_end())
        assert adapter.sends == []
        await emitter.emit(turn_finished_event(AgentResult(content="Hi")))
        assert len(adapter.sends) == 1
        assert adapter.sends[0][0].content == "Hi"

    async def test_empty_text_ignored(self):
        adapter = RecordingOutputAdapter(StreamingMode.NATIVE)
        emitter = _emitter(adapter)
        await emitter.emit(TurnTextEvent(text=""))
        assert adapter.send_deltas == []
        assert emitter._content_buffer == ""

    async def test_streaming_ignores_iteration_end(self):
        """True streaming never buffers, so iteration end has nothing to flush."""
        adapter = RecordingOutputAdapter(StreamingMode.NATIVE)
        emitter = _emitter(adapter)
        await emitter.emit(TurnTextEvent(text="chunk"))
        await emitter.emit(_iteration_end())
        assert adapter.sends == []
        assert adapter.send_deltas == [("chunk", "test_session")]


class TestEventHandling:
    async def test_reasoning_buffers(self):
        adapter = RecordingOutputAdapter(StreamingMode.PSEUDO)
        emitter = _emitter(adapter)
        await emitter.emit(TurnReasoningEvent(text="Let me think... "))
        await emitter.emit(TurnReasoningEvent(text="About this..."))
        assert emitter._reasoning_buffer == "Let me think... About this..."
        assert adapter.sends == []

    async def test_turn_finished_flushes_in_segment_mode(self):
        adapter = RecordingOutputAdapter(StreamingMode.PSEUDO)
        emitter = _emitter(adapter)
        await emitter.emit(TurnTextEvent(text="answer"))
        await emitter.emit(turn_finished_event(AgentResult(content="answer")))
        assert len(adapter.sends) == 1
        assert adapter.sends[0][0].content == "answer"

    async def test_mid_flight_error_sends_error_message(self):
        adapter = RecordingOutputAdapter(StreamingMode.PSEUDO)
        emitter = _emitter(adapter)
        await emitter.emit(TurnErroredEvent(message="boom"))
        assert len(adapter.sends) == 1
        assert "Error: boom" in adapter.sends[0][0].content

    async def test_terminal_error_renders_when_no_mid_flight_error(self):
        adapter = RecordingOutputAdapter(StreamingMode.PSEUDO)
        emitter = _emitter(adapter)
        await emitter.emit(
            turn_finished_event(
                AgentResult(error="late failure", stop_reason=StopReason.ERROR)
            )
        )
        contents = [m.content for m, _ in adapter.sends]
        assert contents == ["Error: late failure"]

    async def test_mid_flight_error_suppresses_terminal_render(self):
        """One user-facing error message per turn — never two."""
        adapter = RecordingOutputAdapter(StreamingMode.PSEUDO)
        emitter = _emitter(adapter)
        await emitter.emit(TurnErroredEvent(message="boom"))
        await emitter.emit(
            turn_finished_event(AgentResult(error="boom", stop_reason=StopReason.ERROR))
        )
        assert len(adapter.sends) == 1
        assert "Error: boom" in adapter.sends[0][0].content

    async def test_tool_events_do_not_touch_adapter(self):
        adapter = RecordingOutputAdapter(StreamingMode.NATIVE)
        emitter = _emitter(adapter)
        await emitter.emit(
            TurnToolCallEvent(tool_name="test_tool", call_id="call-1", arguments={"arg": "value"})
        )
        await emitter.emit(
            TurnToolResultEvent(tool_name="test_tool", call_id="call-1", output="success")
        )
        assert adapter.send_deltas == []
        assert adapter.sends == []

    async def test_repeated_iteration_end_no_duplicate_send(self):
        """A second iteration end after a flush must not re-send empty content."""
        adapter = RecordingOutputAdapter(StreamingMode.PSEUDO)
        emitter = _emitter(adapter)
        await emitter.emit(TurnTextEvent(text="First iteration"))
        await emitter.emit(_iteration_end())
        await emitter.emit(_iteration_end())
        assert len(adapter.sends) == 1
        assert adapter.sends[0][0].content == "First iteration"

    async def test_second_iteration_does_not_accumulate(self):
        adapter = RecordingOutputAdapter(StreamingMode.PSEUDO)
        emitter = _emitter(adapter)
        await emitter.emit(TurnTextEvent(text="First iteration"))
        await emitter.emit(_iteration_end())
        await emitter.emit(TurnTextEvent(text="Second iteration"))
        await emitter.emit(_iteration_end())
        assert len(adapter.sends) == 2
        assert adapter.sends[1][0].content == "Second iteration"
        assert emitter._content_buffer == ""


class TestTurnFinished:
    async def test_finished_flushes_segment_with_reasoning_metadata(self):
        adapter = RecordingOutputAdapter(StreamingMode.PSEUDO)
        emitter = _emitter(adapter)
        await emitter.emit(TurnTextEvent(text="Hello World"))
        await emitter.emit(TurnReasoningEvent(text="Some reasoning"))
        await emitter.emit(
            turn_finished_event(
                AgentResult(content="Hello World", reasoning="Some reasoning")
            )
        )
        assert len(adapter.sends) == 1
        message, session_id = adapter.sends[0]
        assert message.content == "Hello World"
        assert message.metadata.get("reasoning") == "Some reasoning"
        assert session_id == "test_session"
        assert emitter._content_buffer == ""
        assert emitter._reasoning_buffer == ""

    async def test_finished_streaming_clears_buffers_without_send(self):
        adapter = RecordingOutputAdapter(StreamingMode.NATIVE)
        emitter = _emitter(adapter)
        await emitter.emit(TurnTextEvent(text="Hello"))
        await emitter.emit(turn_finished_event(AgentResult(content="Hello")))
        assert adapter.sends == []
        assert emitter._content_buffer == ""

    async def test_flush_method_flushes_segment_buffers(self):
        adapter = RecordingOutputAdapter(StreamingMode.PSEUDO)
        emitter = _emitter(adapter)
        await emitter.emit(TurnTextEvent(text="buffered"))
        await emitter.flush()
        assert len(adapter.sends) == 1
        assert adapter.sends[0][0].content == "buffered"


class TestGapCases:
    """Carried-over gap coverage (plan §18.4)."""

    async def test_per_session_isolation(self):
        """Two sinks on one adapter never see each other's buffers."""
        adapter = RecordingOutputAdapter(StreamingMode.PSEUDO)
        emitter_a = _emitter(adapter, session="sess-a")
        emitter_b = _emitter(adapter, session="sess-b")
        await emitter_a.emit(TurnTextEvent(text="for A"))
        await emitter_b.emit(TurnTextEvent(text="for B"))
        await emitter_a.emit(turn_finished_event(AgentResult(content="for A")))
        await emitter_b.emit(turn_finished_event(AgentResult(content="for B")))
        a_sends = [(m.content, sid) for m, sid in adapter.sends if sid == "sess-a"]
        b_sends = [(m.content, sid) for m, sid in adapter.sends if sid == "sess-b"]
        assert a_sends == [("for A", "sess-a")]
        assert b_sends == [("for B", "sess-b")]

    async def test_exactly_once_flush_on_turn_finished(self):
        """turn_finished must not double-send content already flushed at iteration end."""
        adapter = RecordingOutputAdapter(StreamingMode.PSEUDO)
        emitter = _emitter(adapter)
        await emitter.emit(TurnTextEvent(text="chunk1"))
        await emitter.emit(_iteration_end())  # flush #1
        await emitter.emit(TurnTextEvent(text="chunk2"))
        await emitter.emit(turn_finished_event(AgentResult(content="chunk1chunk2")))  # flush #2 (chunk2 only)
        contents = [m.content for m, _ in adapter.sends]
        assert contents == ["chunk1", "chunk2"]

    async def test_attachment_projection(self):
        """turn_finished forwards attachments as an explicit OutputMessage."""
        adapter = RecordingOutputAdapter(StreamingMode.PSEUDO)
        emitter = _emitter(adapter)
        await emitter.emit(TurnTextEvent(text="done text"))
        await emitter.emit(
            turn_finished_event(
                AgentResult(content="done text", attachments=["a.png", "b.pdf"])
            )
        )
        assert len(adapter.sends) == 2
        assert adapter.sends[0][0].content == "done text"
        attachment_message = adapter.sends[1][0]
        assert attachment_message.content == ""
        assert list(attachment_message.attachments) == ["a.png", "b.pdf"]

    async def test_attachments_sent_even_in_streaming_mode(self):
        adapter = RecordingOutputAdapter(StreamingMode.NATIVE)
        emitter = _emitter(adapter)
        await emitter.emit(turn_finished_event(AgentResult(content="x", attachments=["f.txt"])))
        assert len(adapter.sends) == 1
        assert list(adapter.sends[0][0].attachments) == ["f.txt"]

    async def test_safe_adapter_send_timeout_path(self):
        """A hung adapter send is cut off by send_timeout instead of deadlocking."""
        adapter = SlowOutputAdapter(StreamingMode.PSEUDO)
        emitter = BufferingSink(
            output_adapter=adapter,
            session_id="test_session",
            send_timeout=0.05,
        )
        await emitter.emit(TurnTextEvent(text="data"))
        # Flush goes through _safe_adapter_send; timeout must return, not hang.
        await asyncio.wait_for(emitter.emit(_iteration_end()), timeout=2.0)
        # Buffers are NOT cleared on timeout (flush failed) — but no exception raised.



class TestTerminalTemplate:
    """The _handle_turn_finished template drives every seam in order.

    A subclass overriding the seams must observe exactly one sequence
    per terminal: error render first, then the flush seam (with its
    adapter sends), then attachments, then the hook — with reset always
    running, even when a seam raises.
    """

    class _SeamSpy(BufferingSink):
        def __init__(
            self,
            adapter: RecordingOutputAdapter,
            *,
            fail_flush: bool = False,
            real_flush: bool = False,
        ) -> None:
            super().__init__(adapter, "spy")
            self.calls: list[str] = []
            self._fail_flush = fail_flush
            self._real_flush = real_flush

        async def _render_turn_error(self, message: str) -> None:
            self.calls.append("error")
            await super()._render_turn_error(message)

        async def _flush_on_terminal(self) -> None:
            self.calls.append(f"flush@{len(self.output_adapter.sends)}")
            if self._fail_flush:
                raise RuntimeError("boom")
            if self._real_flush:
                await super()._flush_on_terminal()

        async def _on_turn_finished(self, event: TurnFinishedEvent) -> None:
            # Record how many adapter sends happened BEFORE the hook, so
            # the attachment delivery's position is observable from the
            # call log alone.
            self.calls.append(f"hook@{len(self.output_adapter.sends)}")

        async def _reset_turn_state(self) -> None:
            self.calls.append("reset")
            await super()._reset_turn_state()

    async def test_template_pins_full_terminal_order(self):
        """error render -> flush seam -> attachments -> hook -> reset.

        The send log pins every adjacency: the error message precedes
        the flushed body, the attachment follows the flush, and the hook
        observes all three sends. A re-sequenced template fails on the
        call log OR the contents list.
        """
        adapter = RecordingOutputAdapter(StreamingMode.PSEUDO)
        spy = self._SeamSpy(adapter, real_flush=True)
        await spy.emit(TurnTextEvent(text="body"))
        await spy.emit(
            turn_finished_event(
                AgentResult(
                    content="body",
                    stop_reason=StopReason.ERROR,
                    error="bad",
                    attachments=["f.png"],
                )
            )
        )
        assert spy.calls == ["error", "flush@1", "hook@3", "reset"]
        contents = [m.content for m, _ in adapter.sends]
        assert contents == ["Error: bad", "body", ""]
        assert list(adapter.sends[-1][0].attachments) == ["f.png"]

    async def test_mid_flight_error_suppresses_terminal_render(self):
        """The once flag routes both terminal paths through one helper."""
        adapter = RecordingOutputAdapter(StreamingMode.PSEUDO)
        spy = self._SeamSpy(adapter)
        await spy.emit(TurnErroredEvent(message="mid-flight"))
        await spy.emit(
            turn_finished_event(
                AgentResult(content="x", stop_reason=StopReason.ERROR, error="late")
            )
        )
        # The terminal path ATTEMPTS the render (second "error" marker)
        # but the once flag suppresses its send — one user-facing render.
        assert spy.calls == ["error", "error", "flush@1", "hook@1", "reset"]
        assert [m.content for m, _ in adapter.sends] == ["Error: mid-flight"]

    async def test_two_mid_flight_errors_render_once(self):
        """Declared delta: the plain-sink mid-flight path honors the flag."""
        adapter = RecordingOutputAdapter(StreamingMode.PSEUDO)
        spy = self._SeamSpy(adapter)
        await spy.emit(TurnErroredEvent(message="first"))
        await spy.emit(TurnErroredEvent(message="second"))
        assert [m.content for m, _ in adapter.sends] == ["Error: first"]

    async def test_template_reset_runs_when_seam_raises(self):
        adapter = RecordingOutputAdapter(StreamingMode.PSEUDO)
        spy = self._SeamSpy(adapter, fail_flush=True)
        with contextlib.suppress(RuntimeError):
            await spy.emit(turn_finished_event(AgentResult(content="x")))
        assert spy.calls == ["flush@0", "reset"]
        # the once-flag was reset: a later mid-flight error renders again
        await spy.emit(TurnErroredEvent(message="after"))
        assert [m.content for m, _ in adapter.sends] == ["Error: after"]


class TestKindGate:
    """The gate applies before dispatch (retired EmitterConfig semantics)."""

    async def test_disabled_kind_drops_event(self):
        from modex_agent.core.emitter import KindGate

        adapter = RecordingOutputAdapter(StreamingMode.PSEUDO)
        emitter = BufferingSink(
            adapter,
            "test_session",
            KindGate(disabled_kinds=frozenset({"reasoning"})),
        )
        await emitter.emit(TurnReasoningEvent(text="hidden"))
        assert emitter._reasoning_buffer == ""

    async def test_enabled_kinds_narrow_deliveries(self):
        from modex_agent.core.emitter import KindGate

        adapter = RecordingOutputAdapter(StreamingMode.PSEUDO)
        emitter = BufferingSink(
            adapter,
            "test_session",
            KindGate(enabled_kinds=frozenset({"text", "turn_finished"})),
        )
        await emitter.emit(TurnReasoningEvent(text="dropped"))
        await emitter.emit(TurnTextEvent(text="kept"))
        await emitter.emit(_iteration_end())  # gated off: no flush
        assert emitter._content_buffer == "kept"
        await emitter.emit(turn_finished_event(AgentResult(content="kept")))
        assert [m.content for m, _ in adapter.sends] == ["kept"]
        assert emitter._reasoning_buffer == ""
