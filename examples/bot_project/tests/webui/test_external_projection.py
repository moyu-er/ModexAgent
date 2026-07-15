"""WebBotEmitter projection of ExternalCodingEvent -> ServerEvent (Scheme C).

External coding agents (OpenCode now, Pi-compatible) stream and persist
basic semantic events through the existing WebUI DeltaEnvelope / ServerEvent
/ transcript / materializer path. Provider parsers keep emitting the typed
``Emission`` (the provider-independent external semantic contract); the
WebBotEmitter projects those onto the same ``ServerEvent`` types the ReAct
path uses, so live streaming and transcript replay consume one schema with
no frontend changes and no ReActEvent coupling in the adapters.

The emitter is referenced as a bare ``ContentEmitter`` (the same loose
typing the ``ExternalTurnRunner`` factory uses) because one physical emitter
serves both ReAct and external event enums at runtime.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from bot.adapters.web_socket import WebSocketInputAdapter, WebSocketOutputAdapter
from bot.webui.emitter import WebBotEmitter
from bot.webui.events import (
    AssistantReasoningEvent,
    ToolCallEvent,
    ToolResultEvent,
    WebUIEventType,
)
from bot.webui.transcript_store import JSONLTranscriptStore

from modex_agent.agents.react.agent import ReActEvent
from modex_agent.core.emitter import (
    AgentResult,
    ContentEmitter,
    EmitterConfig,
    StreamingAwareEmitter,
)
from modex_agent.core.tool_manager import ToolResult
from modex_agent.core.turn_events import (
    TurnReasoningEvent,
    TurnTextEvent,
    TurnToolCallEvent,
    TurnToolResultEvent,
)
from modex_agent.core.types import OutputMessage, ToolCall
from modex_agent.pipeline.adapters import OutputAdapter


def _make_emitter(
    tmp: str, session_id: str = "conv1.opencode"
) -> tuple[ContentEmitter, WebSocketInputAdapter, JSONLTranscriptStore, str]:
    input_adapter = WebSocketInputAdapter()
    output_adapter = WebSocketOutputAdapter(input_adapter)
    store = JSONLTranscriptStore(Path(tmp))
    emitter: ContentEmitter = WebBotEmitter(
        output_adapter,
        session_id,
        config=EmitterConfig(),
        transcript_store=store,
    )
    input_adapter.register_connection(session_id, None)
    return emitter, input_adapter, store, session_id


# ---------------------------------------------------------------------------
# Reasoning projection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_external_reasoning_streams_delta_and_persists_event() -> None:
    """THINKING -> ModelReasoningDelta (stream immediately) + buffered
    AssistantReasoningEvent (persisted at flush boundary)."""
    with tempfile.TemporaryDirectory() as tmp:
        emitter, input_adapter, store, sid = _make_emitter(tmp)
        await emitter.emit_turn_event(TurnReasoningEvent(text="reasoning chunk"))

        q = input_adapter._delta_queues.get(sid)
        assert q is not None
        env = q.get_nowait()
        assert env.event_type == WebUIEventType.MODEL_REASONING_DELTA.value
        assert env.payload["text"] == "reasoning chunk"

        # Reasoning is buffered for persistence — not yet in transcript
        assert len(await store.load(sid)) == 0

        await emitter.emit_complete(AgentResult(content=""))

        events = await store.load(sid)
        assert len(events) == 1
        assert isinstance(events[0], AssistantReasoningEvent)
        assert events[0].text == "reasoning chunk"


@pytest.mark.asyncio
async def test_external_reasoning_deltas_coalesced_into_single_event() -> None:
    """Multiple token-level reasoning deltas must coalesce into ONE
    AssistantReasoningEvent."""
    with tempfile.TemporaryDirectory() as tmp:
        emitter, _input_adapter, store, _sid = _make_emitter(tmp)
        for fragment in ["Let", " me", " think", " about", " this."]:
            await emitter.emit_turn_event(TurnReasoningEvent(text=fragment))

        await emitter.emit_complete(AgentResult(content=""))

        events = await store.load("conv1.opencode")
        reasoning_events = [e for e in events if isinstance(e, AssistantReasoningEvent)]
        assert len(reasoning_events) == 1
        assert reasoning_events[0].text == "Let me think about this."


@pytest.mark.asyncio
async def test_interleaved_text_and_reasoning_produce_two_events_not_many() -> None:
    """When text (part_id=p1) and reasoning (part_id=p2) alternate at the
    token level (as opencode SSE does), the emitter must produce exactly 2
    transcript events — one AssistantTextEvent and one AssistantReasoningEvent
    — not one per delta. The part_id state machine tracks each segment
    independently so interleaving doesn't cause flush thrashing."""
    with tempfile.TemporaryDirectory() as tmp:
        emitter, _input_adapter, store, _sid = _make_emitter(tmp)
        for i in range(5):
            await emitter.emit_turn_event(TurnTextEvent(text=f"t{i} ", part_id="p1"))
            await emitter.emit_turn_event(TurnReasoningEvent(text=f"r{i} ", part_id="p2"))

        await emitter.emit_complete(AgentResult(content=""))

        events = await store.load("conv1.opencode")
        text_events = [e for e in events if e.event == "assistant_text"]
        reasoning_events = [e for e in events if e.event == "assistant_reasoning"]
        assert len(text_events) == 1
        assert len(reasoning_events) == 1
        assert text_events[0].text == "t0 t1 t2 t3 t4"
        assert reasoning_events[0].text == "r0 r1 r2 r3 r4"


@pytest.mark.asyncio
async def test_same_part_id_text_deltas_coalesce_across_tool_calls() -> None:
    """Text deltas with the same part_id arriving across tool boundaries
    are tracked as one segment — but a tool call forces a flush of the
    active segment first, so two text segments (same part_id, separated
    by a tool) produce two events."""
    with tempfile.TemporaryDirectory() as tmp:
        emitter, _input_adapter, store, _sid = _make_emitter(tmp)
        await emitter.emit_turn_event(TurnTextEvent(text="before", part_id="p1"))
        await emitter.emit_turn_event(
            TurnToolCallEvent(tool_name="read", arguments={"path": "a"}, call_id="c1")
        )
        await emitter.emit_turn_event(
            TurnToolResultEvent(tool_name="read", call_id="c1", output="ok")
        )
        await emitter.emit_turn_event(TurnTextEvent(text="after", part_id="p1"))
        await emitter.emit_complete(AgentResult(content=""))

        events = await store.load("conv1.opencode")
        text_events = [e for e in events if e.event == "assistant_text"]
        assert len(text_events) == 2
        assert text_events[0].text == "before"
        assert text_events[1].text == "after"


# ---------------------------------------------------------------------------
# Tool projection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_external_tool_start_end_streamed_and_persisted_with_shared_call_id() -> None:
    """TOOL_USE streams tool_call_start; TOOL_RESULT streams tool_call_end and
    persists ToolCallEvent + ToolResultEvent with a matching call_id."""
    with tempfile.TemporaryDirectory() as tmp:
        emitter, input_adapter, store, sid = _make_emitter(tmp)
        call_id = "tc-shared"
        await emitter.emit_turn_event(
            TurnToolCallEvent(
                tool_name="bash", arguments={"cmd": "ls"}, call_id=call_id
            )
        )
        await emitter.emit_turn_event(
            TurnToolResultEvent(
                tool_name="bash", call_id=call_id, output="file.txt"
            )
        )

        q = input_adapter._delta_queues.get(sid)
        assert q is not None
        first = q.get_nowait()
        second = q.get_nowait()
        assert first.event_type == WebUIEventType.TOOL_CALL_START.value
        assert first.payload["tool"] == "bash"
        assert second.event_type == WebUIEventType.TOOL_CALL_END.value

        events = await store.load(sid)
        tc_events = [e for e in events if isinstance(e, ToolCallEvent)]
        tr_events = [e for e in events if isinstance(e, ToolResultEvent)]
        assert len(tc_events) == 1
        assert len(tr_events) == 1
        assert tc_events[0].call_id == call_id
        assert tr_events[0].call_id == call_id
        assert tc_events[0].tool_name == "bash"
        assert tr_events[0].tool_name == "bash"
        assert tr_events[0].result == "file.txt"


@pytest.mark.asyncio
async def test_external_tool_replay_materializes_complete_tool_block() -> None:
    """A text + tool turn replays as one turn with a text block and a complete
    tool block (tool + args + result), paired by call_id."""
    with tempfile.TemporaryDirectory() as tmp:
        emitter, _input_adapter, store, _sid = _make_emitter(tmp)
        await emitter.emit_turn_event(TurnTextEvent(text="Let me check."))
        await emitter.emit_stream_end()
        await emitter.emit_turn_event(
            TurnToolCallEvent(
                tool_name="bash", arguments={"cmd": "ls"}, call_id="tc-1"
            )
        )
        await emitter.emit_turn_event(
            TurnToolResultEvent(
                tool_name="bash", call_id="tc-1", output="file.txt"
            )
        )
        await emitter.emit_complete(AgentResult(content="done"))

        turns = await store.load_materialized_by_prefix("conv1")
        assert len(turns) == 1
        blocks = turns[0].blocks
        kinds = [b["kind"] for b in blocks]
        assert "text" in kinds
        assert "tool" in kinds
        tool_block = next(b for b in blocks if b["kind"] == "tool")
        assert tool_block["tool"] == "bash"
        assert tool_block["args"] == {"cmd": "ls"}
        assert tool_block["result"] == "file.txt"


@pytest.mark.asyncio
async def test_external_full_turn_history_restores_text_reasoning_tool_and_final_text() -> None:
    """A canonical external turn (leading text, reasoning, tool call+result,
    trailing final text) replays through the existing materializer as one
    ordered assistant turn with all four block kinds — the user-facing
    history contract after refresh.
    """
    with tempfile.TemporaryDirectory() as tmp:
        emitter, _input_adapter, store, _sid = _make_emitter(tmp)
        await emitter.emit_turn_event(TurnTextEvent(text="I will inspect."))
        await emitter.emit_stream_end()
        await emitter.emit_turn_event(
            TurnReasoningEvent(text="Need repository context.")
        )
        await emitter.emit_turn_event(
            TurnToolCallEvent(
                tool_name="read",
                arguments={"path": "README.md"},
                call_id="call-1",
            )
        )
        await emitter.emit_turn_event(
            TurnToolResultEvent(
                tool_name="read", call_id="call-1", output="contents"
            )
        )
        await emitter.emit_turn_event(TurnTextEvent(text="Inspection complete."))
        await emitter.emit_complete(AgentResult(content="I will inspect.Inspection complete."))

        turns = await store.load_materialized_by_prefix("conv1")
        assert len(turns) == 1
        blocks = turns[0].blocks
        kinds = [b["kind"] for b in blocks]

        assert kinds == ["text", "reasoning", "tool", "text"]

        reasoning_block = next(b for b in blocks if b["kind"] == "reasoning")
        assert reasoning_block["text"] == "Need repository context."

        tool_block = next(b for b in blocks if b["kind"] == "tool")
        assert tool_block["tool"] == "read"
        assert tool_block["args"] == {"path": "README.md"}
        assert tool_block["result"] == "contents"

        text_blocks = [b for b in blocks if b["kind"] == "text"]
        assert text_blocks[-1]["text"] == "Inspection complete."


@pytest.mark.asyncio
async def test_external_tool_result_without_use_persists_result_only() -> None:
    """An orphan TOOL_RESULT (no preceding TOOL_USE) still persists a
    ToolResultEvent and does not crash the projection."""
    with tempfile.TemporaryDirectory() as tmp:
        emitter, _input_adapter, store, _sid = _make_emitter(tmp)
        await emitter.emit_turn_event(
            TurnToolResultEvent(
                tool_name="bash", call_id="orphan-1", output="late"
            )
        )
        await emitter.emit_complete(AgentResult(content="done"))

        events = await store.load("conv1.opencode")
        assert any(isinstance(e, ToolResultEvent) for e in events)


# ---------------------------------------------------------------------------
# Text ordering: text emitted before non-text events must appear before them
# in the transcript (ExternalCodingAgent does NOT call emit_stream_end between
# text and tool events — the emitter must flush text in-order itself).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_text_before_tool_call_preserves_order_in_transcript() -> None:
    """Text emitted via TurnTextEvent (without an intervening emit_stream_end)
    must appear BEFORE the subsequent tool_call in the persisted transcript.

    This simulates the real ExternalCodingAgent._handle_emission flow: it
    emits TurnTextEvent then TurnToolCallEvent with no emit_stream_end call
    between them.  The emitter must flush its text buffer before persisting
    the tool event so chronological order is preserved.
    """
    with tempfile.TemporaryDirectory() as tmp:
        emitter, _input_adapter, store, _sid = _make_emitter(tmp)
        await emitter.emit_turn_event(TurnTextEvent(text="Let me check the file."))
        await emitter.emit_turn_event(
            TurnToolCallEvent(
                tool_name="read", arguments={"path": "a.txt"}, call_id="c1"
            )
        )
        await emitter.emit_turn_event(
            TurnToolResultEvent(
                tool_name="read", call_id="c1", output="contents"
            )
        )
        await emitter.emit_complete(AgentResult(content="done"))

        events = await store.load("conv1.opencode")
        event_types = [str(e.event) for e in events]

        assert "assistant_text" in event_types, f"Missing assistant_text; got: {event_types}"
        assert "tool_call" in event_types, f"Missing tool_call; got: {event_types}"

        text_idx = event_types.index("assistant_text")
        tool_idx = event_types.index("tool_call")
        assert text_idx < tool_idx, (
            f"assistant_text (idx={text_idx}) must appear before tool_call "
            f"(idx={tool_idx}) in transcript; got order: {event_types}"
        )


# ---------------------------------------------------------------------------
# ReAct no-regression guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_text_between_tool_calls_preserves_position_in_transcript() -> None:
    """Text emitted between two tool calls must appear between them in the
    transcript, not aggregated at the end.

    Simulates: text → tool1 → tool1_result → text → tool2 → tool2_result → done.
    Without the flush fix, both text blocks would be merged and persisted
    after all tool events.
    """
    with tempfile.TemporaryDirectory() as tmp:
        emitter, _input_adapter, store, _sid = _make_emitter(tmp)
        await emitter.emit_turn_event(TurnTextEvent(text="Starting."))
        await emitter.emit_turn_event(
            TurnToolCallEvent(tool_name="read", arguments={"path": "a"}, call_id="c1")
        )
        await emitter.emit_turn_event(
            TurnToolResultEvent(tool_name="read", call_id="c1", output="ra")
        )
        await emitter.emit_turn_event(TurnTextEvent(text="Now the second file."))
        await emitter.emit_turn_event(
            TurnToolCallEvent(tool_name="read", arguments={"path": "b"}, call_id="c2")
        )
        await emitter.emit_turn_event(
            TurnToolResultEvent(tool_name="read", call_id="c2", output="rb")
        )
        await emitter.emit_complete(AgentResult(content="done"))

        events = await store.load("conv1.opencode")
        event_types = [str(e.event) for e in events]

        text_indices = [i for i, t in enumerate(event_types) if t == "assistant_text"]
        assert len(text_indices) == 2, f"Expected 2 assistant_text events, got {len(text_indices)}; order: {event_types}"

        tool1_idx = event_types.index("tool_call")
        tool2_idx = event_types.index("tool_call", tool1_idx + 1)

        assert text_indices[0] < tool1_idx, f"First text must be before first tool_call; got: {event_types}"
        assert tool1_idx < text_indices[1] < tool2_idx, (
            f"Second text must be between the two tool_calls; got: {event_types}"
        )


# ---------------------------------------------------------------------------
# ReAct no-regression guard (original)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_react_reasoning_and_tool_projection_unchanged() -> None:
    """The ReAct MODEL_REASONING / TOOL_CALL_START / TOOL_CALL_END path is
    unaffected by the external projection additions."""
    with tempfile.TemporaryDirectory() as tmp:
        emitter, _input_adapter, store, sid = _make_emitter(tmp, "conv1.main")
        await emitter.emit(ReActEvent.MODEL_REASONING, "think")
        tc = ToolCall(tool_name="read_file", arguments={"path": "/x"}, call_id="call_0")
        await emitter.emit(ReActEvent.TOOL_CALL_START, tc)
        await emitter.emit(
            ReActEvent.TOOL_CALL_END,
            (tc, ToolResult(tool_name="read_file", result="content")),
        )
        await emitter.emit_complete(AgentResult(content="done"))

        events = await store.load(sid)
        assert any(isinstance(e, AssistantReasoningEvent) for e in events)
        tc_events = [e for e in events if isinstance(e, ToolCallEvent)]
        tr_events = [e for e in events if isinstance(e, ToolResultEvent)]
        assert len(tc_events) == 1
        assert len(tr_events) == 1
        assert tc_events[0].call_id == tr_events[0].call_id == "call_0"


# ---------------------------------------------------------------------------
# IM no-op guard
# ---------------------------------------------------------------------------


class _RecordingOutputAdapter(OutputAdapter):
    """Minimal OutputAdapter that records every sent message content."""

    def __init__(self) -> None:
        self.sends: list[str] = []

    @property
    def name(self) -> str:
        return "recording"

    async def send(self, message: OutputMessage, session_id: str) -> None:
        self.sends.append(message.content or "")


@pytest.mark.asyncio
async def test_default_emitter_noops_structured_external_events() -> None:
    """A plain StreamingAwareEmitter (the IM base) does not forward structured
    external events and does not error on them. IM keeps receiving only text
    via emit_delta / emit_complete, matching existing ReAct tool behavior."""
    adapter = _RecordingOutputAdapter()
    emitter: ContentEmitter = StreamingAwareEmitter(
        output_adapter=adapter,
        session_id="im1.main",
    )
    await emitter.emit_turn_event(TurnReasoningEvent(text="hmm"))
    await emitter.emit_turn_event(
        TurnToolCallEvent(tool_name="bash", arguments={}, call_id="x")
    )
    await emitter.emit_turn_event(
        TurnToolResultEvent(tool_name="bash", call_id="x", output="ok")
    )

    # No OutputMessage was forwarded for these structured external events.
    assert adapter.sends == []
