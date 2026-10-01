"""New/legacy transcript replay equivalence (the W6 cutover acceptance).

For one scripted turn the same user-visible history must come back whether
the records were written through the NEW path (the recording tap persisting
``PresentationEvent`` records, replayed through the framework
``materialize_turns``) or through the LEGACY path (the pre-cutover writer's
``ServerEvent`` records on disk, replayed through the legacy read adapter).

Comparison happens at the API-response-shape level the history route
serves (``GET /api/sessions/{id}/messages``): user messages as-is plus
synthetic ``assistant_turn`` dicts with blocks/attachments, and — for the
mid-stream refresh case — the synthetic streaming turn from the partial
buffer. Volatile identity (wall-clock timestamps, opaque turn ids) is
normalized away; block content, order, tool pairing, and error flags must
match exactly.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

import pytest
from bot.adapters.web_socket import WebSocketInputAdapter, WebSocketOutputAdapter
from bot.webui.emitter import WebBotEmitter
from bot.webui.events import (
    AssistantTextEvent,
    ToolCallEvent,
    ToolResultEvent,
    UserMessageEvent,
)
from bot.webui.routes.sessions.messages import (
    _user_message_json,
    partial_streaming_turn,
)
from bot.webui.transcript_store import (
    JSONLTranscriptStore,
    UserMessageRecord,
    materialize_records,
)

from modex_agent.core.emitter import AgentResult, turn_finished_event
from modex_agent.core.turn_events import (
    StopReason,
    TurnTextEvent,
    TurnToolCallEvent,
    TurnToolResultEvent,
)

pytestmark = pytest.mark.asyncio

_SID = "conv.main"


# ── The history-route response projection (mirrors handle_get_messages) ─────


def _history_response(records: list[Any], agent_name: str) -> list[dict[str, Any]]:
    """Project transcript records to the history-API response entries.

    The same projection ``handle_get_messages`` applies: user-message
    records serialized as-is, materialized turns as synthetic
    ``assistant_turn`` dicts, merged by timestamp.
    """
    user_events = [
        _user_message_json(record)
        for record in records
        if isinstance(record, UserMessageRecord)
    ]
    assistant_events: list[dict[str, Any]] = []
    for turn in materialize_records(records):
        assistant_events.append(
            {
                "event": "assistant_turn",
                "session_id": _SID,
                "agent_name": agent_name,
                "timestamp": turn.started_at,
                "turn_id": turn.turn_id,
                "blocks": turn.blocks,
                "latency_ms": 0,
                "attachments": turn.attachments,
            }
        )
    result = user_events + assistant_events
    result.sort(key=lambda event: int(str(event.get("timestamp", 0) or 0)))
    return result


def _normalize(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop volatile identity (timestamps, opaque turn ids) from a response."""
    normalized: list[dict[str, Any]] = []
    for event in events:
        entry = dict(event)
        entry.pop("timestamp", None)
        if entry.get("event") == "assistant_turn":
            # Turn ids are opaque per generation; only their pairing with
            # content matters. Keep a boolean "has one".
            entry["turn_id"] = bool(entry.get("turn_id"))
        normalized.append(entry)
    return normalized


# ── The scripted turn, written through both generations ─────────────────────


async def _write_new_generation(base_dir: Path) -> JSONLTranscriptStore:
    """Drive the real recording tap (WebBotEmitter) through the scripted turns."""
    input_adapter = WebSocketInputAdapter()
    emitter = WebBotEmitter(
        WebSocketOutputAdapter(input_adapter), _SID, transcript_store=None
    )
    # Swap in the store after construction (the emitter holds it mutably via
    # the protected member the same test suite already exercises).
    store = JSONLTranscriptStore(base_dir)
    emitter._transcript_store = store
    input_adapter.register_connection(_SID, None)

    # Turn 1: reasoning + text → tool → error tool → closing text.
    await emitter.emit(_reasoning("Thinking about it"))
    await emitter.emit(_text("Let me check."))
    await emitter.emit(
        TurnToolCallEvent(tool_name="read_file", call_id="c1", arguments={"path": "/x"})
    )
    await emitter.emit(
        TurnToolResultEvent(tool_name="read_file", call_id="c1", output="contents", seq=0)
    )
    await emitter.emit(_text("Now writing."))
    await emitter.emit(
        TurnToolCallEvent(tool_name="rm", call_id="c2", arguments={"path": "/x"})
    )
    await emitter.emit(
        TurnToolResultEvent(
            tool_name="rm", call_id="c2", output="", error="Permission denied", seq=1
        )
    )
    await emitter.emit(_text("Done."))
    await emitter.emit(
        turn_finished_event(AgentResult(stop_reason=StopReason.COMPLETED, content="Done."))
    )

    # Turn 2: parallel tools completing OUT of model order (B before A) —
    # pins the seq-ordering equivalence.
    await emitter.emit(_text("Running two tools."))
    await emitter.emit(
        TurnToolCallEvent(tool_name="toolB", call_id="cB", arguments={"n": 1})
    )
    await emitter.emit(
        TurnToolResultEvent(tool_name="toolB", call_id="cB", output="B-out", seq=1)
    )
    await emitter.emit(
        TurnToolCallEvent(tool_name="toolA", call_id="cA", arguments={"n": 0})
    )
    await emitter.emit(
        TurnToolResultEvent(tool_name="toolA", call_id="cA", output="A-out", seq=0)
    )
    await emitter.emit(
        turn_finished_event(AgentResult(stop_reason=StopReason.COMPLETED, content="ok"))
    )

    # The user message rides the same store (the S7 writer's record).
    await store.append(
        _SID,
        UserMessageRecord(session_id=_SID, agent_name="main", timestamp_ms=1, content="hello"),
    )
    return store


def _legacy_lines() -> list[str]:
    """The same scripted turns as pre-cutover ``ServerEvent`` JSONL lines.

    Exactly what today's (retired) writer would have persisted: the user
    message record, per-segment ``assistant_text``/``assistant_reasoning``
    flushes with stripped text, and call+result event pairs sharing a
    turn id.
    """
    events: list[UserMessageEvent | AssistantTextEvent | ToolCallEvent | ToolResultEvent] = [
        UserMessageEvent(session_id=_SID, agent_name="main", content="hello", timestamp=1),
        # Turn 1 (legacy writer never persisted turn_start/turn_end).
        _legacy_reasoning("t1", "Thinking about it", 100),
        _legacy_text("t1", "Let me check.", 110),
        ToolCallEvent(session_id=_SID, agent_name="main", turn_id="t1",
                      call_id="c1", tool_name="read_file", args={"path": "/x"}, timestamp=120),
        ToolResultEvent(session_id=_SID, agent_name="main", turn_id="t1",
                        call_id="c1", tool_name="read_file", result="contents",
                        seq=0, timestamp=130),
        _legacy_text("t1", "Now writing.", 140),
        ToolCallEvent(session_id=_SID, agent_name="main", turn_id="t1",
                      call_id="c2", tool_name="rm", args={"path": "/x"}, timestamp=150),
        ToolResultEvent(session_id=_SID, agent_name="main", turn_id="t1",
                        call_id="c2", tool_name="rm", result="",
                        error="Permission denied", seq=1, timestamp=160),
        _legacy_text("t1", "Done.", 170),
        # Turn 2: parallel completion order B then A (seq re-orders them).
        _legacy_text("t2", "Running two tools.", 200),
        ToolCallEvent(session_id=_SID, agent_name="main", turn_id="t2",
                      call_id="cB", tool_name="toolB", args={"n": 1}, timestamp=210),
        ToolResultEvent(session_id=_SID, agent_name="main", turn_id="t2",
                        call_id="cB", tool_name="toolB", result="B-out",
                        seq=1, timestamp=220),
        ToolCallEvent(session_id=_SID, agent_name="main", turn_id="t2",
                      call_id="cA", tool_name="toolA", args={"n": 0}, timestamp=230),
        ToolResultEvent(session_id=_SID, agent_name="main", turn_id="t2",
                        call_id="cA", tool_name="toolA", result="A-out",
                        seq=0, timestamp=240),
    ]
    return [json.dumps(event.to_dict(), ensure_ascii=False) for event in events]


def _legacy_reasoning(turn_id: str, text: str, ts: int) -> AssistantTextEvent:
    from bot.webui.events import AssistantReasoningEvent

    return AssistantReasoningEvent(
        session_id=_SID, agent_name="main", turn_id=turn_id, text=text, timestamp=ts
    )


def _legacy_text(turn_id: str, text: str, ts: int) -> AssistantTextEvent:
    return AssistantTextEvent(
        session_id=_SID, agent_name="main", turn_id=turn_id, text=text, timestamp=ts
    )


def _text(content: str) -> TurnTextEvent:
    return TurnTextEvent(text=content)


def _reasoning(content: str) -> Any:
    from modex_agent.core.turn_events import TurnReasoningEvent

    return TurnReasoningEvent(text=content)


# ── Acceptance: full-turn replay equivalence ────────────────────────────────


async def test_full_turn_replay_equivalence_new_vs_legacy() -> None:
    """(a) NEW records replayed through the framework materializer serve the
    SAME user-visible history as (b) legacy records replayed through the
    legacy read adapter — same turns, blocks, order, tool pairing, error
    flags."""
    with tempfile.TemporaryDirectory() as tmp_new, tempfile.TemporaryDirectory() as tmp_old:
        new_store = await _write_new_generation(Path(tmp_new))

        old_base = Path(tmp_old)
        (old_base / f"{_SID}.jsonl").write_text(
            "\n".join(_legacy_lines()) + "\n", encoding="utf-8"
        )
        legacy_store = JSONLTranscriptStore(old_base)

        new_response = _normalize(_history_response(await new_store.load(_SID), "main"))
        legacy_response = _normalize(
            _history_response(await legacy_store.load(_SID), "main")
        )

        assert legacy_response == [
            {
                "event": "user_message",
                "session_id": _SID,
                "agent_name": "main",
                "content": "hello",
                "attachments": [],
            },
            {
                "event": "assistant_turn",
                "session_id": _SID,
                "agent_name": "main",
                "turn_id": True,
                "blocks": [
                    {"kind": "reasoning", "text": "Thinking about it"},
                    {"kind": "text", "text": "Let me check."},
                    {"kind": "tool", "tool": "read_file", "args": {"path": "/x"},
                     "result": "contents"},
                    {"kind": "text", "text": "Now writing."},
                    {"kind": "tool", "tool": "rm", "args": {"path": "/x"},
                     "result": "Error: Permission denied"},
                    {"kind": "text", "text": "Done."},
                ],
                "latency_ms": 0,
                "attachments": [],
            },
            {
                "event": "assistant_turn",
                "session_id": _SID,
                "agent_name": "main",
                "turn_id": True,
                "blocks": [
                    {"kind": "text", "text": "Running two tools."},
                    # seq 0 (toolA) re-orders ahead of seq 1 (toolB) even
                    # though B completed first — model order wins.
                    {"kind": "tool", "tool": "toolA", "args": {"n": 0},
                     "result": "A-out"},
                    {"kind": "tool", "tool": "toolB", "args": {"n": 1},
                     "result": "B-out"},
                ],
                "latency_ms": 0,
                "attachments": [],
            },
        ]
        assert new_response == legacy_response


async def test_new_path_never_persists_transient_kinds() -> None:
    """tool_args_delta stays transient: the warm-up signal never reaches
    the store (the unchanged W6 invariant)."""
    from modex_agent.core.turn_events import ToolArgsDeltaEvent

    with tempfile.TemporaryDirectory() as tmp:
        input_adapter = WebSocketInputAdapter()
        emitter = WebBotEmitter(
            WebSocketOutputAdapter(input_adapter), _SID, transcript_store=None
        )
        store = JSONLTranscriptStore(Path(tmp))
        emitter._transcript_store = store
        input_adapter.register_connection(_SID, None)

        await emitter.emit(
            ToolArgsDeltaEvent(call_id="c1", tool_name="read", args_fragment='{"a":')
        )
        await emitter.emit(
            TurnToolCallEvent(tool_name="read", call_id="c1", arguments={"a": 1})
        )
        await emitter.emit(
            TurnToolResultEvent(tool_name="read", call_id="c1", output="ok", seq=0)
        )
        await emitter.emit(
            turn_finished_event(AgentResult(content="ok"))
        )

        records = await store.load(_SID)
        assert [record.kind for record in records] == [
            "tool_call_started",
            "tool_result",
            "turn_finished",
        ]


# ── Acceptance: partial-resume (mid-stream refresh) equivalence ─────────────


async def test_partial_resume_equivalence_mid_stream() -> None:
    """A refresh mid-stream shows the same partial turn as the retired
    ServerEvent partial fold produced.

    (a) NEW: the partial buffer holds the streamed TextDelta/ThinkingDelta
    records; ``partial_streaming_turn`` (the route's fold over the framework
    materializer) builds the synthetic streaming turn.
    (b) LEGACY: the expected dict per the retired ``_materialize_partial_deltas``
    behavior (merge by segment, order of first appearance, is_streaming,
    min timestamp, first non-empty turn id).
    """

    with tempfile.TemporaryDirectory() as tmp:
        input_adapter = WebSocketInputAdapter()
        emitter = WebBotEmitter(
            WebSocketOutputAdapter(input_adapter), _SID, transcript_store=None
        )
        from bot.service.workspace_store import WorkspaceScopedTranscriptStore

        scoped = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
        sessions_dir = Path(tmp)
        emitter._transcript_store = scoped
        emitter.set_sessions_dir_provider(lambda: sessions_dir)
        input_adapter.register_connection(_SID, None)

        # Mid-stream: reasoning + partial text streamed, no boundary yet.
        await emitter.emit(_reasoning("Thinking..."))
        await emitter.emit(_text("Answer so "))
        await emitter.emit(_text("far"))

        partial_records = await scoped.load_partial(_SID, sessions_dir=sessions_dir)
        assert [record.kind for record in partial_records] == [
            "thinking_delta",
            "text_delta",
            "text_delta",
        ]

        new_partial = partial_streaming_turn(partial_records, "main")

        # The retired fold's exact user-visible contract.
        assert new_partial is not None
        assert new_partial["event"] == "assistant_turn"
        assert new_partial["session_id"] == ""
        assert new_partial["agent_name"] == "main"
        assert new_partial["is_streaming"] is True
        assert new_partial["latency_ms"] == 0
        assert bool(new_partial["turn_id"]) is True
        assert new_partial["blocks"] == [
            {"kind": "reasoning", "text": "Thinking..."},
            {"kind": "text", "text": "Answer so far"},
        ]
        # timestamp = the FIRST partial record's time (min), as before.
        first_ts = min(
            record.timestamp_ms
            for record in partial_records
            if record.timestamp_ms is not None
        )
        assert new_partial["timestamp"] == first_ts


async def test_partial_resume_after_flush_shows_tail_only() -> None:
    """Refresh AFTER a tool boundary: the flushed text is a materialized
    turn and the partial buffer holds only the post-flush tail — no
    duplication, exactly the pre-cutover refresh behavior."""
    with tempfile.TemporaryDirectory() as tmp:
        input_adapter = WebSocketInputAdapter()
        emitter = WebBotEmitter(
            WebSocketOutputAdapter(input_adapter), _SID, transcript_store=None
        )
        from bot.service.workspace_store import WorkspaceScopedTranscriptStore

        scoped = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
        sessions_dir = Path(tmp)
        emitter._transcript_store = scoped
        emitter.set_sessions_dir_provider(lambda: sessions_dir)
        input_adapter.register_connection(_SID, None)

        await emitter.emit(_text("text before tool"))
        await emitter.emit(
            TurnToolCallEvent(tool_name="read", call_id="c1", arguments={"q": "x"})
        )
        await emitter.emit(
            TurnToolResultEvent(tool_name="read", call_id="c1", output="ok", seq=0)
        )
        await emitter.emit(_text("streaming tail"))

        # Materialized (flushed) turn from the durable records.
        from bot.webui.transcript_store import AttachmentCarrier

        durable = [
            record
            for record in await scoped.load(_SID, sessions_dir=sessions_dir)
            if not isinstance(record, AttachmentCarrier)
        ]
        turns = materialize_records(durable)
        assert len(turns) == 1
        assert turns[0].blocks == [
            {"kind": "text", "text": "text before tool"},
            {"kind": "tool", "tool": "read", "args": {"q": "x"}, "result": "ok"},
        ]

        # The partial buffer holds ONLY the post-flush tail.
        partial_records = await scoped.load_partial(_SID, sessions_dir=sessions_dir)
        partial = partial_streaming_turn(partial_records, "main")
        assert partial is not None
        assert partial["blocks"] == [{"kind": "text", "text": "streaming tail"}]
        assert partial["is_streaming"] is True
