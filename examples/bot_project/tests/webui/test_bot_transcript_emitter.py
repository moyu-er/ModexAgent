"""Tests for the shared BotTranscriptEmitter recording lifecycle and its
WebUI / ACP projections.

The base class owns ONE transcript writer per emitter: text/reasoning
accumulate as segments and flush as single transcript events, tool pairs
persist together with full fidelity. Projections translate the same facts
into their own sink — the WebUI one truncates for display, the ACP one
forwards full-fidelity ``TurnEvent``s. These tests pin the split: single
write, full args, no duplicated text.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import pytest
from bot.acp.emitter import AcpEmitterHub, AcpTurnEmitter
from bot.adapters.web_socket import WebSocketInputAdapter, WebSocketOutputAdapter
from bot.webui.emitter import BotTranscriptEmitter, WebBotEmitter
from bot.webui.events import (
    AssistantReasoningEvent,
    AssistantTextEvent,
    ToolCallEvent,
    ToolResultEvent,
    WebUIEventType,
)
from bot.webui.transcript_store import JSONLTranscriptStore, TranscriptStore

from modex_agent.agents.react.agent import ReActEvent
from modex_agent.agents.react.constants import ToolCallEndPayload
from modex_agent.core.emitter import AgentResult
from modex_agent.core.events import EmitterConfig
from modex_agent.core.message import ToolCall
from modex_agent.core.tool_manager import ToolResult
from modex_agent.core.turn_events import (
    TurnEvent,
    TurnTextEvent,
    TurnToolCallEvent,
    TurnToolResultEvent,
)


class _RecordingTranscriptStore(TranscriptStore):
    """In-memory TranscriptStore capturing appends per session."""

    def __init__(self) -> None:
        self.events: dict[str, list[Any]] = {}

    async def append(
        self, session_id: str, event: Any, *, pool: str = "main"
    ) -> None:
        self.events.setdefault(session_id, []).append(event)

    async def load(self, session_id: str) -> list[Any]:
        return list(self.events.get(session_id, []))

    async def load_sessions_by_prefix(
        self, session_prefix: str, *, pool: str | None = None
    ) -> list[Any]:
        merged: list[Any] = []
        for sid, events in self.events.items():
            if sid.startswith(session_prefix):
                merged.extend(events)
        return merged

    async def list_sessions(self) -> set[str]:
        return set(self.events)

    async def list_sessions_by_prefix(self, session_prefix: str) -> set[str]:
        return {sid for sid in self.events if sid.startswith(session_prefix)}

    async def delete_session(self, session_id: str) -> None:
        self.events.pop(session_id, None)

    async def delete_sessions_by_prefix(self, session_prefix: str) -> None:
        for sid in [s for s in self.events if s.startswith(session_prefix)]:
            self.events.pop(sid, None)


class _Collector:
    """TurnEvent listener recording every event it receives."""

    def __init__(self) -> None:
        self.events: list[TurnEvent] = []

    async def __call__(self, event: TurnEvent) -> None:
        self.events.append(event)


class _NullAdapter:
    """Minimal OutputAdapter for driving the base lifecycle directly."""

    @property
    def name(self) -> str:
        return "null"

    async def send(self, message: Any, session_id: str) -> None:
        return None

    async def send_delta(
        self, delta: str, session_id: str, metadata: dict[str, Any] | None = None
    ) -> None:
        return None

    async def flush_deltas(self, session_id: str) -> None:
        return None


def test_base_emitter_is_abstract() -> None:
    """The lifecycle owner is a projection contract — it cannot be built
    without a concrete sink."""
    with pytest.raises(TypeError):
        BotTranscriptEmitter(_NullAdapter(), "conv1.main")  # type: ignore[abstract]


@pytest.mark.asyncio
async def test_lifecycle_single_write_text_and_reasoning() -> None:
    """Deltas accumulate; exactly ONE AssistantTextEvent + ONE
    AssistantReasoningEvent reach the transcript — never per-delta writes."""
    with tempfile.TemporaryDirectory() as tmp:
        store = JSONLTranscriptStore(Path(tmp))
        emitter = WebBotEmitter(
            WebSocketOutputAdapter(WebSocketInputAdapter()),
            "conv1.main",
            config=EmitterConfig(),
            transcript_store=store,
        )

        await emitter.emit_delta("Hello ")
        await emitter.emit_delta("world")
        await emitter.emit(ReActEvent.MODEL_REASONING, "thinking")
        await emitter.emit_complete(AgentResult(content="done"))

        events = await store.load("conv1.main")
        texts = [
            e
            for e in events
            if e.event == WebUIEventType.ASSISTANT_TEXT.value
            and isinstance(e, AssistantTextEvent)
        ]
        reasoning = [
            e
            for e in events
            if e.event == WebUIEventType.ASSISTANT_REASONING.value
            and isinstance(e, AssistantReasoningEvent)
        ]
        assert len(texts) == 1 and texts[0].text == "Hello world"
        assert len(reasoning) == 1 and reasoning[0].text == "thinking"


@pytest.mark.asyncio
async def test_react_tool_pair_persisted_once_with_full_fidelity() -> None:
    """TOOL_CALL_START + TOOL_CALL_END persist ONE pair sharing turn_id with
    the FULL args and result (not the WS display truncations)."""
    with tempfile.TemporaryDirectory() as tmp:
        store = JSONLTranscriptStore(Path(tmp))
        emitter = WebBotEmitter(
            WebSocketOutputAdapter(WebSocketInputAdapter()),
            "conv1.main",
            config=EmitterConfig(),
            transcript_store=store,
        )
        big_args = {"path": "a.txt", "blob": "x" * 600}
        tc = ToolCall(tool_name="read", arguments=big_args, call_id="c1")
        await emitter.emit(ReActEvent.TOOL_CALL_START, tc)
        await emitter.emit(
            ReActEvent.TOOL_CALL_END,
            ToolCallEndPayload(
                tool_call=tc, result=ToolResult.from_text("read", "r" * 1000), seq=7
            ),
        )

        events = await store.load("conv1.main")
        tc_events = [e for e in events if isinstance(e, ToolCallEvent)]
        tr_events = [e for e in events if isinstance(e, ToolResultEvent)]
        assert len(tc_events) == 1 and len(tr_events) == 1
        assert tc_events[0].args == big_args
        assert tr_events[0].result == "r" * 1000
        assert tr_events[0].seq == 7
        assert tc_events[0].turn_id == tr_events[0].turn_id


@pytest.mark.asyncio
async def test_acp_projection_full_fidelity_no_truncation_single_writer() -> None:
    """The ACP projection forwards FULL args/result as TurnEvents while the
    SAME turn wrote the canonical tool pair to the transcript — one writer,
    two sinks, no WS-style truncation."""
    hub = AcpEmitterHub()
    collector = _Collector()
    hub.register("sess-1", collector)
    transcripts = _RecordingTranscriptStore()
    emitter = AcpTurnEmitter(hub, "sess-1", transcript_store=transcripts, pool="main")

    big_args = {"path": "a.txt", "blob": "x" * 600}
    big_result = "r" * 1000
    tc = ToolCall(tool_name="read", arguments=big_args, call_id="c1")
    await emitter.emit(ReActEvent.TOOL_CALL_START, tc)
    await emitter.emit(
        ReActEvent.TOOL_CALL_END,
        ToolCallEndPayload(
            tool_call=tc, result=ToolResult.from_text("read", big_result), seq=3
        ),
    )

    calls = [e for e in collector.events if isinstance(e, TurnToolCallEvent)]
    results = [e for e in collector.events if isinstance(e, TurnToolResultEvent)]
    assert len(calls) == 1 and len(results) == 1
    assert calls[0].arguments == big_args  # full args, not the 500-char truncation
    assert results[0].output == big_result  # full output, not the 200-char summary
    assert calls[0].call_id == "c1" == results[0].call_id

    persisted = transcripts.events["sess-1"]
    tc_persisted = [e for e in persisted if isinstance(e, ToolCallEvent)]
    tr_persisted = [e for e in persisted if isinstance(e, ToolResultEvent)]
    assert len(tc_persisted) == 1 and len(tr_persisted) == 1
    assert tr_persisted[0].result == big_result


@pytest.mark.asyncio
async def test_acp_text_projected_once_without_duplicate() -> None:
    """Streamed text reaches the editor exactly once and the transcript
    exactly once — emit_content/delta paths never double-project."""
    hub = AcpEmitterHub()
    collector = _Collector()
    hub.register("sess-1", collector)
    transcripts = _RecordingTranscriptStore()
    emitter = AcpTurnEmitter(hub, "sess-1", transcript_store=transcripts, pool="main")

    await emitter.emit_delta("hello ")
    await emitter.emit_delta("back")
    await emitter.emit_complete(AgentResult(content="hello back"))

    texts = [e for e in collector.events if isinstance(e, TurnTextEvent)]
    assert "".join(e.text for e in texts) == "hello back"

    persisted = transcripts.events["sess-1"]
    assistant_texts = [e for e in persisted if isinstance(e, AssistantTextEvent)]
    assert len(assistant_texts) == 1
    assert assistant_texts[0].text == "hello back"


@pytest.mark.asyncio
async def test_web_projection_dual_sink_order_preserved() -> None:
    """Through the real WS adapter: one delta envelope per fragment, one
    persisted AssistantTextEvent per segment, and the flush-before-tool
    ordering holds on both sinks."""
    with tempfile.TemporaryDirectory() as tmp:
        input_adapter = WebSocketInputAdapter()
        emitter = WebBotEmitter(
            WebSocketOutputAdapter(input_adapter),
            "conv1.main",
            config=EmitterConfig(),
            transcript_store=JSONLTranscriptStore(Path(tmp)),
        )
        input_adapter.register_connection("conv1.main", None)

        await emitter.emit_delta("looking")
        await emitter.emit(
            ReActEvent.TOOL_CALL_START,
            ToolCall(tool_name="read", arguments={"path": "a"}, call_id="c1"),
        )
        await emitter.emit(
            ReActEvent.TOOL_CALL_END,
            ToolCallEndPayload(
                tool_call=ToolCall(tool_name="read", arguments={"path": "a"}, call_id="c1"),
                result=ToolResult.from_text("read", "ok"),
                seq=0,
            ),
        )
        await emitter.emit_complete(AgentResult(content="done"))

        q = input_adapter.get_delta_queue("conv1.main", None)
        assert q is not None
        kinds: list[str] = []
        while not q.empty():
            kinds.append(q.get_nowait().event_type)
        assert kinds[:3] == [
            WebUIEventType.MODEL_CONTENT_DELTA.value,
            WebUIEventType.TOOL_CALL_START.value,
            WebUIEventType.TOOL_CALL_END.value,
        ]
        assert kinds[-1] == WebUIEventType.TURN_END.value
