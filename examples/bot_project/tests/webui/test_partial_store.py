"""Tests for the in-memory partial streaming record buffer.

Partial records (TextDelta / ThinkingDelta) are held in an in-memory dict
on ``WorkspaceScopedTranscriptStore`` during streaming and cleared on turn
completion. Process crash drops the whole buffer (no leftover, no startup
sweep needed). The main transcript store (SQLite or file) never sees them
— two independent stores, two separate queries.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from bot.service.workspace_store import WorkspaceScopedTranscriptStore
from bot.webui.routes.sessions.messages import partial_streaming_turn
from bot.webui.transcript_store import UserMessageRecord

from modex_agent.presentation import TextDelta, ThinkingDelta

pytestmark = pytest.mark.asyncio


def _content_delta(session_id: str, text: str, *, segment_id: str = "_text", turn_id: str = "t1", ts: int = 100) -> TextDelta:
    return TextDelta(
        session_id=session_id,
        agent_name="main",
        text=text,
        turn_id=turn_id,
        segment_id=segment_id,
        timestamp_ms=ts,
    )


def _reasoning_delta(session_id: str, text: str, *, segment_id: str = "_reasoning", turn_id: str = "t1", ts: int = 100) -> ThinkingDelta:
    return ThinkingDelta(
        session_id=session_id,
        agent_name="main",
        text=text,
        turn_id=turn_id,
        segment_id=segment_id,
        timestamp_ms=ts,
    )


# ── WorkspaceScopedTranscriptStore in-memory partial buffer ─────────────────


async def test_append_and_load_partial() -> None:
    store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
    sessions_dir = Path(__file__).parent / "_tmp_mem_partial"
    sid = "abc.main"
    try:
        await store.append_partial(sid, _content_delta(sid, "Hello", ts=100), sessions_dir=sessions_dir)
        await store.append_partial(sid, _content_delta(sid, " world", ts=101), sessions_dir=sessions_dir)
        partials = await store.load_partial(sid, sessions_dir=sessions_dir)
        assert len(partials) == 2
        assert partials[0].kind == "text_delta"
        assert partials[1].kind == "text_delta"
    finally:
        import shutil
        shutil.rmtree(sessions_dir, ignore_errors=True)


async def test_load_partial_empty_when_no_buffer() -> None:
    store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
    sessions_dir = Path(__file__).parent / "_tmp_mem_empty"
    try:
        assert await store.load_partial("nonexistent.main", sessions_dir=sessions_dir) == []
    finally:
        import shutil
        shutil.rmtree(sessions_dir, ignore_errors=True)


async def test_clear_partial_removes_buffer() -> None:
    store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
    sessions_dir = Path(__file__).parent / "_tmp_mem_clear"
    sid = "abc.main"
    try:
        await store.append_partial(sid, _content_delta(sid, "Hello"), sessions_dir=sessions_dir)
        assert await store.load_partial(sid, sessions_dir=sessions_dir) != []
        await store.clear_partial(sid, sessions_dir=sessions_dir)
        assert await store.load_partial(sid, sessions_dir=sessions_dir) == []
    finally:
        import shutil
        shutil.rmtree(sessions_dir, ignore_errors=True)


async def test_partial_does_not_leak_into_main_transcript() -> None:
    store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
    sessions_dir = Path(__file__).parent / "_tmp_mem_leak"
    sid = "abc.main"
    try:
        await store.append_partial(sid, _content_delta(sid, "streaming delta"), sessions_dir=sessions_dir)
        await store.append(sid, UserMessageRecord(session_id=sid, agent_name="main", content="hi"), sessions_dir=sessions_dir)
        events = await store.load_sessions_by_prefix("abc", sessions_dir=sessions_dir)
        assert all(e.kind != "text_delta" for e in events)
        assert any(e.kind == "user_message" for e in events)
    finally:
        import shutil
        shutil.rmtree(sessions_dir, ignore_errors=True)


async def test_partial_isolated_per_workspace() -> None:
    store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
    sid = "abc.main"
    ws_a = Path(__file__).parent / "_tmp_ws_a"
    ws_b = Path(__file__).parent / "_tmp_ws_b"
    try:
        await store.append_partial(sid, _content_delta(sid, "from ws A"), sessions_dir=ws_a)
        await store.append_partial(sid, _content_delta(sid, "from ws B"), sessions_dir=ws_b)
        a_partials = await store.load_partial(sid, sessions_dir=ws_a)
        b_partials = await store.load_partial(sid, sessions_dir=ws_b)
        assert len(a_partials) == 1
        assert len(b_partials) == 1
        assert a_partials[0].text == "from ws A"
        assert b_partials[0].text == "from ws B"
        await store.clear_partial(sid, sessions_dir=ws_a)
        assert await store.load_partial(sid, sessions_dir=ws_a) == []
        assert len(await store.load_partial(sid, sessions_dir=ws_b)) == 1
    finally:
        import shutil
        shutil.rmtree(ws_a, ignore_errors=True)
        shutil.rmtree(ws_b, ignore_errors=True)


async def test_partial_isolated_per_session() -> None:
    store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
    sessions_dir = Path(__file__).parent / "_tmp_mem_session"
    try:
        await store.append_partial("abc.main", _content_delta("abc.main", "main turn"), sessions_dir=sessions_dir)
        await store.append_partial("abc.reviewer.aa11", _content_delta("abc.reviewer.aa11", "reviewer turn"), sessions_dir=sessions_dir)
        main = await store.load_partial("abc.main", sessions_dir=sessions_dir)
        reviewer = await store.load_partial("abc.reviewer.aa11", sessions_dir=sessions_dir)
        assert len(main) == 1
        assert len(reviewer) == 1
        assert main[0].text == "main turn"
        assert reviewer[0].text == "reviewer turn"
    finally:
        import shutil
        shutil.rmtree(sessions_dir, ignore_errors=True)


async def test_load_partial_returns_snapshot_not_live_ref() -> None:
    store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
    sessions_dir = Path(__file__).parent / "_tmp_mem_snapshot"
    sid = "abc.main"
    try:
        await store.append_partial(sid, _content_delta(sid, "Hello"), sessions_dir=sessions_dir)
        snapshot = await store.load_partial(sid, sessions_dir=sessions_dir)
        snapshot.clear()
        again = await store.load_partial(sid, sessions_dir=sessions_dir)
        assert len(again) == 1
    finally:
        import shutil
        shutil.rmtree(sessions_dir, ignore_errors=True)


# ── partial_streaming_turn (the route's synthetic streaming turn) ──────────


async def test_partial_streaming_turn_single_text_segment() -> None:
    sid = "abc.main"
    records = [
        _content_delta(sid, "Hello", segment_id="_text", ts=100),
        _content_delta(sid, " world", segment_id="_text", ts=101),
    ]
    result = partial_streaming_turn(records, "main")
    assert result is not None
    assert result["event"] == "assistant_turn"
    assert result["is_streaming"] is True
    assert result["agent_name"] == "main"
    blocks = result["blocks"]
    assert len(blocks) == 1
    assert blocks[0]["kind"] == "text"
    assert blocks[0]["text"] == "Hello world"


async def test_partial_streaming_turn_reasoning_and_text() -> None:
    sid = "abc.main"
    records = [
        _reasoning_delta(sid, "Thinking...", segment_id="_reasoning", ts=100),
        _content_delta(sid, "Answer", segment_id="_text", ts=101),
    ]
    result = partial_streaming_turn(records, "main")
    assert result is not None
    blocks = result["blocks"]
    assert len(blocks) == 2
    assert blocks[0]["kind"] == "reasoning"
    assert blocks[0]["text"] == "Thinking..."
    assert blocks[1]["kind"] == "text"
    assert blocks[1]["text"] == "Answer"


async def test_partial_streaming_turn_empty_returns_none() -> None:
    assert partial_streaming_turn([], "main") is None


async def test_partial_streaming_turn_carries_turn_id_and_first_timestamp() -> None:
    sid = "abc.main"
    records = [
        _content_delta(sid, "Hi", turn_id="turn_42", ts=300),
        _content_delta(sid, " there", turn_id="turn_42", ts=400),
    ]
    result = partial_streaming_turn(records, "main")
    assert result is not None
    assert result["turn_id"] == "turn_42"
    assert result["timestamp"] == 300


# ── End-to-end: WebBotEmitter clears partial on turn_finished ────────────────


async def test_turn_finished_clears_partial_buffer() -> None:
    """Verify _clear_partial is actually called when turn_finished runs.

    Uses a real WebBotEmitter + WorkspaceScopedTranscriptStore to exercise
    the full turn lifecycle: TurnTextEvent emit (writes partial) →
    TurnFinishedEvent (must clear partial). If _clear_partial is never
    wired or skipped, the buffer will still hold the delta after
    turn_finished.
    """
    from bot.adapters.web_socket import WebSocketInputAdapter, WebSocketOutputAdapter
    from bot.webui.emitter import WebBotEmitter

    from modex_agent.core.emitter import AgentResult, turn_finished_event
    from modex_agent.core.turn_events import TurnTextEvent

    store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
    sessions_dir = Path(__file__).parent / "_tmp_e2e_clear"
    sid = "abc.main"

    input_adapter = WebSocketInputAdapter()
    output_adapter = WebSocketOutputAdapter(input_adapter)
    input_adapter.register_connection(sid, None)

    emitter = WebBotEmitter(
        output_adapter, sid,
        transcript_store=store,
    )
    # Wire the sessions_dir provider so partial writes route to the right workspace
    emitter.set_sessions_dir_provider(lambda: sessions_dir)

    try:
        await emitter.emit(TurnTextEvent(text="Hello"))
        # Partial buffer should hold the delta mid-turn
        partials = await store.load_partial(sid, sessions_dir=sessions_dir)
        assert len(partials) == 1, "partial buffer should hold delta mid-turn"
        assert partials[0].text == "Hello"

        await emitter.emit(turn_finished_event(AgentResult(content="Hello")))
        # After turn_finished, partial buffer MUST be empty
        after = await store.load_partial(sid, sessions_dir=sessions_dir)
        assert after == [], f"partial buffer must be cleared after turn_finished, got {after}"
    finally:
        import shutil
        shutil.rmtree(sessions_dir, ignore_errors=True)


async def test_turn_finished_clears_partial_even_on_error() -> None:
    """Verify _clear_partial runs in the finally block — even when
    turn_finished's main body raises, the buffer is still cleared."""
    from bot.adapters.web_socket import WebSocketInputAdapter, WebSocketOutputAdapter
    from bot.webui.emitter import WebBotEmitter

    from modex_agent.core.emitter import AgentResult, turn_finished_event
    from modex_agent.core.turn_events import TurnTextEvent

    store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
    sessions_dir = Path(__file__).parent / "_tmp_e2e_error"
    sid = "abc.main"

    input_adapter = WebSocketInputAdapter()
    output_adapter = WebSocketOutputAdapter(input_adapter)
    input_adapter.register_connection(sid, None)

    emitter = WebBotEmitter(
        output_adapter, sid,
        transcript_store=store,
    )
    emitter.set_sessions_dir_provider(lambda: sessions_dir)

    try:
        await emitter.emit(TurnTextEvent(text="Hello"))
        assert len(await store.load_partial(sid, sessions_dir=sessions_dir)) == 1

        import unittest.mock as mock
        emitter._flush_active_segment = mock.AsyncMock(side_effect=RuntimeError("flush broken"))

        with pytest.raises(RuntimeError):
            await emitter.emit(turn_finished_event(AgentResult(content="Hello")))

        after = await store.load_partial(sid, sessions_dir=sessions_dir)
        assert after == [], "partial buffer must be cleared even when turn_finished raises"
    finally:
        import shutil
        shutil.rmtree(sessions_dir, ignore_errors=True)


# ── Regression: flush clears partial buffer (prevents duplicate content) ────


async def test_flush_active_segment_clears_partial_buffer() -> None:
    """Regression: _flush_active_segment must clear the partial buffer.

    Without this, the partial buffer retains ALL deltas for the entire turn
    — including text already persisted as AssistantTextEvent — so
    _materialize_partial_deltas produces a synthetic streaming turn whose
    single concatenated text block duplicates the materialized transcript
    turn's text. The user sees the same content twice on session re-select.
    """
    from bot.adapters.web_socket import WebSocketInputAdapter, WebSocketOutputAdapter
    from bot.webui.emitter import WebBotEmitter

    from modex_agent.core.emitter import AgentResult, turn_finished_event
    from modex_agent.core.turn_events import (
        TurnTextEvent,
        TurnToolCallEvent,
        TurnToolResultEvent,
    )

    store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
    sessions_dir = Path(__file__).parent / "_tmp_flush_clear"
    sid = "abc.main"

    input_adapter = WebSocketInputAdapter()
    output_adapter = WebSocketOutputAdapter(input_adapter)
    input_adapter.register_connection(sid, None)

    emitter = WebBotEmitter(
        output_adapter, sid,
        transcript_store=store,
    )
    emitter.set_sessions_dir_provider(lambda: sessions_dir)

    try:
        await emitter.emit(TurnTextEvent(text="text before tool"))
        assert len(await store.load_partial(sid, sessions_dir=sessions_dir)) == 1

        await emitter.emit(
            TurnToolCallEvent(
                tool_name="read_file", call_id="c0", arguments={"path": "/x"}
            )
        )
        await emitter.emit(
            TurnToolResultEvent(
                tool_name="read_file", call_id="c0", output="ok", seq=0
            )
        )

        partials = await store.load_partial(sid, sessions_dir=sessions_dir)
        assert partials == [], (
            "partial buffer must be empty after _flush_active_segment (tool call boundary); "
            f"got {len(partials)} stale deltas"
        )

        await emitter.emit(TurnTextEvent(text="text after tool"))
        assert len(await store.load_partial(sid, sessions_dir=sessions_dir)) == 1

        await emitter.emit(turn_finished_event(AgentResult(content="done")))
        assert await store.load_partial(sid, sessions_dir=sessions_dir) == []
    finally:
        import shutil
        shutil.rmtree(sessions_dir, ignore_errors=True)


async def test_flush_clears_partial_with_reasoning_then_text() -> None:
    """Reasoning deltas followed by text deltas must also clear partial on flush."""
    from bot.adapters.web_socket import WebSocketInputAdapter, WebSocketOutputAdapter
    from bot.webui.emitter import WebBotEmitter

    from modex_agent.core.emitter import AgentResult, turn_finished_event
    from modex_agent.core.turn_events import (
        IterationFinishedEvent,
        TurnReasoningEvent,
    )

    store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
    sessions_dir = Path(__file__).parent / "_tmp_flush_reasoning"
    sid = "abc.main"

    input_adapter = WebSocketInputAdapter()
    output_adapter = WebSocketOutputAdapter(input_adapter)
    input_adapter.register_connection(sid, None)

    emitter = WebBotEmitter(
        output_adapter, sid,
        transcript_store=store,
    )
    emitter.set_sessions_dir_provider(lambda: sessions_dir)

    try:
        await emitter.emit(TurnReasoningEvent(text="thinking"))
        assert len(await store.load_partial(sid, sessions_dir=sessions_dir)) == 1

        await emitter.emit(IterationFinishedEvent(iteration=0, has_tool_calls=False))

        partials = await store.load_partial(sid, sessions_dir=sessions_dir)
        assert partials == [], (
            "partial buffer must be empty after iteration_finished segment flush; "
            f"got {len(partials)} stale deltas"
        )

        await emitter.emit(turn_finished_event(AgentResult(content="done")))
    finally:
        import shutil
        shutil.rmtree(sessions_dir, ignore_errors=True)
