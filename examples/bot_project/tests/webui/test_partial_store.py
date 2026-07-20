"""Tests for the in-memory partial streaming event buffer.

Partial events (ModelContentDelta / ModelReasoningDelta) are held in an
in-memory dict on ``WorkspaceScopedTranscriptStore`` during streaming and
cleared on turn completion. Process crash drops the whole buffer (no
leftover, no startup sweep needed). The main transcript store (SQLite or
file) never sees them — two independent stores, two separate queries.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bot.webui.events import (
    ModelContentDelta,
    ModelReasoningDelta,
    UserMessageEvent,
)
from bot.service.workspace_store import WorkspaceScopedTranscriptStore

pytestmark = pytest.mark.asyncio


def _content_delta(session_id: str, text: str, *, segment_id: str = "_text", turn_id: str = "t1", ts: int = 100) -> ModelContentDelta:
    return ModelContentDelta(
        session_id=session_id,
        agent_name="main",
        text=text,
        turn_id=turn_id,
        segment_id=segment_id,
        timestamp=ts,
    )


def _reasoning_delta(session_id: str, text: str, *, segment_id: str = "_reasoning", turn_id: str = "t1", ts: int = 100) -> ModelReasoningDelta:
    return ModelReasoningDelta(
        session_id=session_id,
        agent_name="main",
        text=text,
        turn_id=turn_id,
        segment_id=segment_id,
        timestamp=ts,
    )


# ── WorkspaceScopedTranscriptStore in-memory partial buffer ─────────────────


async def test_append_and_load_partial() -> None:
    store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
    store.set_agent_pool_map({"main": "main"})
    sessions_dir = Path(__file__).parent / "_tmp_mem_partial"
    sid = "abc.main"
    try:
        await store.append_partial(sid, _content_delta(sid, "Hello", ts=100), sessions_dir=sessions_dir)
        await store.append_partial(sid, _content_delta(sid, " world", ts=101), sessions_dir=sessions_dir)
        partials = await store.load_partial(sid, sessions_dir=sessions_dir)
        assert len(partials) == 2
        assert partials[0].event == "model_content_delta"
        assert partials[1].event == "model_content_delta"
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
    store.set_agent_pool_map({"main": "main"})
    sessions_dir = Path(__file__).parent / "_tmp_mem_leak"
    sid = "abc.main"
    try:
        await store.append_partial(sid, _content_delta(sid, "streaming delta"), sessions_dir=sessions_dir)
        await store.append(sid, UserMessageEvent(session_id=sid, agent_name="main", content="hi"), sessions_dir=sessions_dir)
        events = await store.load_sessions_by_prefix("abc", sessions_dir=sessions_dir)
        assert all(e.event != "model_content_delta" for e in events)
        assert any(e.event == "user_message" for e in events)
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


# ── _materialize_partial_deltas ─────────────────────────────────────────────


async def test_materialize_partial_deltas_single_text_segment() -> None:
    from bot.webui.server import _materialize_partial_deltas

    sid = "abc.main"
    events = [
        _content_delta(sid, "Hello", segment_id="_text", ts=100),
        _content_delta(sid, " world", segment_id="_text", ts=101),
    ]
    result = _materialize_partial_deltas(events, "main")
    assert result is not None
    assert result["event"] == "assistant_turn"
    assert result["is_streaming"] is True
    assert result["agent_name"] == "main"
    blocks = result["blocks"]
    assert len(blocks) == 1
    assert blocks[0]["kind"] == "text"
    assert blocks[0]["text"] == "Hello world"


async def test_materialize_partial_deltas_reasoning_and_text() -> None:
    from bot.webui.server import _materialize_partial_deltas

    sid = "abc.main"
    events = [
        _reasoning_delta(sid, "Thinking...", segment_id="_reasoning", ts=100),
        _content_delta(sid, "Answer", segment_id="_text", ts=101),
    ]
    result = _materialize_partial_deltas(events, "main")
    assert result is not None
    blocks = result["blocks"]
    assert len(blocks) == 2
    assert blocks[0]["kind"] == "reasoning"
    assert blocks[0]["text"] == "Thinking..."
    assert blocks[1]["kind"] == "text"
    assert blocks[1]["text"] == "Answer"


async def test_materialize_partial_deltas_empty_returns_none() -> None:
    from bot.webui.server import _materialize_partial_deltas

    assert _materialize_partial_deltas([], "main") is None


async def test_materialize_partial_deltas_carries_turn_id() -> None:
    from bot.webui.server import _materialize_partial_deltas

    sid = "abc.main"
    events = [_content_delta(sid, "Hi", turn_id="turn_42")]
    result = _materialize_partial_deltas(events, "main")
    assert result is not None
    assert result["turn_id"] == "turn_42"


# ── End-to-end: WebBotEmitter clears partial on emit_complete ───────────────


async def test_emit_complete_clears_partial_buffer() -> None:
    """Verify _clear_partial is actually called when emit_complete runs.

    Uses a real WebBotEmitter + WorkspaceScopedTranscriptStore to exercise
    the full turn lifecycle: emit_delta (writes partial) → emit_complete
    (must clear partial). If _clear_partial is never wired or skipped,
    the buffer will still hold the delta after emit_complete.
    """
    from bot.adapters.web_socket import WebSocketInputAdapter, WebSocketOutputAdapter
    from bot.webui.emitter import WebBotEmitter
    from modex_agent.core.emitter import AgentResult, EmitterConfig

    store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
    store.set_agent_pool_map({"main": "main"})
    sessions_dir = Path(__file__).parent / "_tmp_e2e_clear"
    sid = "abc.main"

    input_adapter = WebSocketInputAdapter()
    output_adapter = WebSocketOutputAdapter(input_adapter)
    input_adapter.register_connection(sid, None)

    emitter = WebBotEmitter(
        output_adapter, sid,
        config=EmitterConfig(),
        transcript_store=store,
    )
    # Wire the sessions_dir provider so partial writes route to the right workspace
    emitter.set_sessions_dir_provider(lambda: sessions_dir)

    try:
        await emitter.emit_delta("Hello")
        # Partial buffer should hold the delta mid-turn
        partials = await store.load_partial(sid, sessions_dir=sessions_dir)
        assert len(partials) == 1, "partial buffer should hold delta mid-turn"
        assert partials[0].text == "Hello"

        await emitter.emit_complete(AgentResult(content="Hello"))
        # After emit_complete, partial buffer MUST be empty
        after = await store.load_partial(sid, sessions_dir=sessions_dir)
        assert after == [], f"partial buffer must be cleared after emit_complete, got {after}"
    finally:
        import shutil
        shutil.rmtree(sessions_dir, ignore_errors=True)


async def test_emit_complete_clears_partial_even_on_error() -> None:
    """Verify _clear_partial runs in the finally block — even when
    emit_complete's main body raises, the buffer is still cleared."""
    from bot.adapters.web_socket import WebSocketInputAdapter, WebSocketOutputAdapter
    from bot.webui.emitter import WebBotEmitter
    from modex_agent.core.emitter import AgentResult, EmitterConfig

    store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
    store.set_agent_pool_map({"main": "main"})
    sessions_dir = Path(__file__).parent / "_tmp_e2e_error"
    sid = "abc.main"

    input_adapter = WebSocketInputAdapter()
    output_adapter = WebSocketOutputAdapter(input_adapter)
    input_adapter.register_connection(sid, None)

    emitter = WebBotEmitter(
        output_adapter, sid,
        config=EmitterConfig(),
        transcript_store=store,
    )
    emitter.set_sessions_dir_provider(lambda: sessions_dir)

    try:
        await emitter.emit_delta("Hello")
        assert len(await store.load_partial(sid, sessions_dir=sessions_dir)) == 1

        import unittest.mock as mock
        emitter._flush_active_segment = mock.AsyncMock(side_effect=RuntimeError("flush broken"))

        with pytest.raises(RuntimeError):
            await emitter.emit_complete(AgentResult(content="Hello"))

        after = await store.load_partial(sid, sessions_dir=sessions_dir)
        assert after == [], "partial buffer must be cleared even when emit_complete raises"
    finally:
        import shutil
        shutil.rmtree(sessions_dir, ignore_errors=True)
