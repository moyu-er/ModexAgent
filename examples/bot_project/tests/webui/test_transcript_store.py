"""Tests for the session_id-keyed TranscriptStore (JSONL)."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from bot.webui.events import (
    AssistantTurnEvent,
    ModelContentDelta,
    ServerEvent,
    UserMessageEvent,
)
from bot.webui.transcript_store import JSONLTranscriptStore, TranscriptStore


# ── Helpers ────────────────────────────────────────────────────────────────


def _make_store() -> JSONLTranscriptStore:
    return JSONLTranscriptStore(Path(tempfile.mkdtemp()))


def _msg(session_id: str, content: str = "hi", **kwargs: object) -> UserMessageEvent:
    return UserMessageEvent(
        session_id=session_id,
        agent_name=kwargs.get("agent_name", "main"),
        content=content,
        timestamp=kwargs.get("timestamp", 100.0),
    )


# ── Core: append / load keyed by full session_id ──────────────────────────


def test_append_and_load_events() -> None:
    store: TranscriptStore = _make_store()
    store.append("abc.main", _msg("abc.main"))
    store.append(
        "abc.main",
        ModelContentDelta(session_id="abc.main", agent_name="main", text="hello", turn_id="t1"),
    )
    events = list(store.load("abc.main"))
    assert len(events) == 2
    assert events[0].event == "user_message"
    assert events[1].event == "model_content_delta"


def test_load_empty_session_returns_nothing() -> None:
    store: TranscriptStore = _make_store()
    assert list(store.load("nonexistent.main")) == []


def test_two_subagent_invocations_persist_to_separate_sessions() -> None:
    """Regression: two reviewer invocations must NOT collapse into one file.

    The real session_id carries an invocation_id segment
    (``{conv}.{agent}.{invocation_id}``). The store must key by the FULL
    session_id so each invocation is independently persisted and loadable.
    """
    store: TranscriptStore = _make_store()
    store.append("conv.reviewer.aa11", _msg("conv.reviewer.aa11", "review 1"))
    store.append("conv.reviewer.bb22", _msg("conv.reviewer.bb22", "review 2"))

    first = list(store.load("conv.reviewer.aa11"))
    second = list(store.load("conv.reviewer.bb22"))
    assert len(first) == 1
    assert len(second) == 1
    assert first[0].content == "review 1"  # type: ignore[attr-defined]
    assert second[0].content == "review 2"  # type: ignore[attr-defined]


# ── Listing ────────────────────────────────────────────────────────────────


def test_list_sessions_returns_full_session_ids() -> None:
    store: TranscriptStore = _make_store()
    store.append("abc.main", _msg("abc.main", agent_name="main"))
    store.append("abc.office-expert", _msg("abc.office-expert", agent_name="office-expert"))
    sessions = store.list_sessions()
    assert sessions == {"abc.main", "abc.office-expert"}


def test_list_sessions_in_conversation_groups_by_prefix() -> None:
    store: TranscriptStore = _make_store()
    store.append("abc.main", _msg("abc.main"))
    store.append("abc.reviewer.zz99", _msg("abc.reviewer.zz99"))
    store.append("xyz.main", _msg("xyz.main"))
    sessions = store.list_sessions_in_conversation("abc")
    assert sessions == {"abc.main", "abc.reviewer.zz99"}


# ── load_conversation (merge across sessions by timestamp) ─────────────────


def test_load_conversation_merges_sessions_by_timestamp() -> None:
    store: TranscriptStore = _make_store()
    store.append("conv.main", _msg("conv.main", "hi", timestamp=100.0))
    store.append(
        "conv.main",
        AssistantTurnEvent(
            session_id="conv.main",
            agent_name="main",
            blocks=[{"kind": "text", "text": "hello"}],
            turn_id="t1",
            latency_ms=500,
            timestamp=200.0,
        ),
    )
    store.append(
        "conv.reviewer.aa",
        AssistantTurnEvent(
            session_id="conv.reviewer.aa",
            agent_name="reviewer",
            blocks=[{"kind": "text", "text": "review"}],
            turn_id="t1",
            latency_ms=300,
            timestamp=150.0,
        ),
    )

    all_events = list(store.load_conversation("conv"))
    assert len(all_events) == 3
    assert all_events[0].event == "user_message"
    assert all_events[1].agent_name == "reviewer"  # t=150
    assert all_events[2].agent_name == "main"  # t=200


def test_load_conversation_empty_returns_nothing() -> None:
    store: TranscriptStore = _make_store()
    assert list(store.load_conversation("nonexistent")) == []


# ── Delete ─────────────────────────────────────────────────────────────────


def test_delete_session_removes_only_that_session() -> None:
    store: TranscriptStore = _make_store()
    store.append("abc.main", _msg("abc.main"))
    store.append("abc.reviewer.aa", _msg("abc.reviewer.aa"))
    store.delete_session("abc.main")
    assert list(store.load("abc.main")) == []
    assert len(list(store.load("abc.reviewer.aa"))) == 1


def test_delete_conversation_removes_all_sessions_in_conversation() -> None:
    store: TranscriptStore = _make_store()
    store.append("abc.main", _msg("abc.main"))
    store.append("abc.reviewer.aa", _msg("abc.reviewer.aa"))
    store.append("xyz.main", _msg("xyz.main"))
    store.delete_conversation("abc")
    assert store.list_sessions_in_conversation("abc") == set()
    assert "xyz.main" in store.list_sessions()


# ── Round-trip & migration ─────────────────────────────────────────────────


def test_assistant_turn_roundtrip() -> None:
    store: TranscriptStore = _make_store()
    ev = AssistantTurnEvent(
        session_id="conv1.main",
        agent_name="main",
        blocks=[
            {"kind": "reasoning", "text": "The user said hi"},
            {"kind": "text", "text": "Hello"},
            {"kind": "tool", "tool": "read", "args": {"path": "x"}, "result": "ok"},
        ],
        turn_id="turn_1",
        latency_ms=500,
    )
    store.append("conv1.main", ev)
    loaded = list(store.load("conv1.main"))
    assert len(loaded) == 1
    assert loaded[0].event == "assistant_turn"
    blocks = loaded[0].blocks  # type: ignore[attr-defined]
    assert len(blocks) == 3
    assert blocks[2] == {"kind": "tool", "tool": "read", "args": {"path": "x"}, "result": "ok"}


def test_old_format_assistant_turn_migrated_on_load(tmp_path: Path) -> None:
    """Old-format assistant_turn (content/reasoning/tools) migrates to blocks."""
    store = JSONLTranscriptStore(tmp_path)
    old_event = {
        "event": "assistant_turn",
        "conversation_id": "conv1",
        "agent_name": "main",
        "timestamp": 1718234567.0,
        "content": "Hello World",
        "reasoning": "The user said hi",
        "tools": [
            {"tool": "read", "args": {"path": "README.md"}, "result": "file content..."},
        ],
        "turn_id": "turn_1",
        "latency_ms": 3000,
    }
    # File named by the full main-agent session_id (matches canonical format).
    (tmp_path / "conv1.main.jsonl").write_text(
        json.dumps(old_event, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    loaded = list(store.load("conv1.main"))
    assert len(loaded) == 1
    blocks = loaded[0].blocks  # type: ignore[attr-defined]
    assert len(blocks) == 3
    assert blocks[0] == {"kind": "reasoning", "text": "The user said hi"}
    assert blocks[1] == {"kind": "text", "text": "Hello World"}
    assert blocks[2]["tool"] == "read"
