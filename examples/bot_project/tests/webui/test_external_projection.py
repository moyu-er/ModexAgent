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
    """THINKING -> ModelReasoningDelta (stream) + AssistantReasoningEvent (persist)."""
    with tempfile.TemporaryDirectory() as tmp:
        emitter, input_adapter, store, sid = _make_emitter(tmp)
        await emitter.emit_turn_event(TurnReasoningEvent(text="reasoning chunk"))

        q = input_adapter._delta_queues.get(sid)
        assert q is not None
        env = q.get_nowait()
        assert env.event_type == WebUIEventType.MODEL_REASONING_DELTA.value
        assert env.payload["text"] == "reasoning chunk"

        events = list(store.load(sid))
        assert len(events) == 1
        assert isinstance(events[0], AssistantReasoningEvent)
        assert events[0].text == "reasoning chunk"


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

        events = list(store.load(sid))
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

        turns = store.load_materialized_by_prefix("conv1")
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

        turns = store.load_materialized_by_prefix("conv1")
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

        events = list(store.load("conv1.opencode"))
        assert any(isinstance(e, ToolResultEvent) for e in events)


# ---------------------------------------------------------------------------
# ReAct no-regression guard
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

        events = list(store.load(sid))
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
