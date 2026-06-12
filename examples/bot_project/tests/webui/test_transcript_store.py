"""Tests for TranscriptStore implementations (JSONL + SQLite)."""

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
from bot.webui.transcript_store import (
    JSONLTranscriptStore,
    SQLiteTranscriptStore,
    TranscriptStore,
)


# ── Helpers ────────────────────────────────────────────────────────────────


def _make_jsonl_store() -> JSONLTranscriptStore:
    tmp = tempfile.mkdtemp()
    return JSONLTranscriptStore(Path(tmp))


def _make_sqlite_store() -> SQLiteTranscriptStore:
    tmp = tempfile.mkdtemp()
    return SQLiteTranscriptStore(Path(tmp) / "transcript.db")


# Parametrize both implementations so every test runs against both.
@pytest.mark.parametrize("store_factory", [_make_jsonl_store, _make_sqlite_store])
def test_append_and_load_events(
    store_factory: object,
) -> None:
    store: TranscriptStore = store_factory()  # type: ignore[operator]
    store.append("abc", "main", UserMessageEvent(conversation_id="abc", agent_name="main", content="hi"))
    store.append("abc", "main", ModelContentDelta(conversation_id="abc", agent_name="main", text="hello", turn_id="t1"))
    events = list(store.load("abc", "main"))
    assert len(events) == 2
    assert events[0].event == "user_message"
    assert events[1].event == "model_content_delta"


@pytest.mark.parametrize("store_factory", [_make_jsonl_store, _make_sqlite_store])
def test_list_conversations_and_agents(
    store_factory: object,
) -> None:
    store: TranscriptStore = store_factory()  # type: ignore[operator]
    store.append("abc", "main", UserMessageEvent(conversation_id="abc", agent_name="main", content="hi"))
    store.append("abc", "office-expert", UserMessageEvent(conversation_id="abc", agent_name="office-expert", content="hi"))
    convs = store.list_conversations()
    assert convs == {"abc"}
    agents = store.list_agents("abc")
    assert agents == {"main", "office-expert"}


@pytest.mark.parametrize("store_factory", [_make_jsonl_store, _make_sqlite_store])
def test_delete_conversation(
    store_factory: object,
) -> None:
    store: TranscriptStore = store_factory()  # type: ignore[operator]
    store.append("abc", "main", UserMessageEvent(conversation_id="abc", agent_name="main", content="hi"))
    store.delete_conversation("abc")
    assert store.list_conversations() == set()


@pytest.mark.parametrize("store_factory", [_make_jsonl_store, _make_sqlite_store])
def test_load_empty_conversation_returns_nothing(
    store_factory: object,
) -> None:
    store: TranscriptStore = store_factory()  # type: ignore[operator]
    events = list(store.load("nonexistent", "main"))
    assert events == []


@pytest.mark.parametrize("store_factory", [_make_jsonl_store, _make_sqlite_store])
def test_assistant_turn_roundtrip(
    store_factory: object,
) -> None:
    store: TranscriptStore = store_factory()  # type: ignore[operator]
    ev = AssistantTurnEvent(
        conversation_id="conv1",
        agent_name="main",
        blocks=[
            {"kind": "reasoning", "text": "The user said hi"},
            {"kind": "text", "text": "Hello"},
            {"kind": "tool", "tool": "read", "args": {"path": "x"}, "result": "ok"},
        ],
        turn_id="turn_1",
        latency_ms=500,
    )
    store.append("conv1", "main", ev)
    loaded = list(store.load("conv1", "main"))
    assert len(loaded) == 1
    assert loaded[0].event == "assistant_turn"
    blocks = loaded[0].blocks  # type: ignore[attr-defined]
    assert len(blocks) == 3
    assert blocks[0] == {"kind": "reasoning", "text": "The user said hi"}
    assert blocks[1] == {"kind": "text", "text": "Hello"}
    assert blocks[2] == {"kind": "tool", "tool": "read", "args": {"path": "x"}, "result": "ok"}


def test_old_format_assistant_turn_migrated_on_load(
    tmp_path: Path,
) -> None:
    """Old-format assistant_turn (content/reasoning/tools) is migrated to blocks on load."""
    store = JSONLTranscriptStore(tmp_path)
    # Simulate an old-format JSONL line written by the previous version.
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
    conv_dir = tmp_path / "conv1"
    conv_dir.mkdir()
    (conv_dir / "main.jsonl").write_text(
        json.dumps(old_event, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    loaded = list(store.load("conv1", "main"))
    assert len(loaded) == 1
    assert loaded[0].event == "assistant_turn"
    blocks = loaded[0].blocks  # type: ignore[attr-defined]
    # Migration order: reasoning → content → tools
    assert len(blocks) == 3
    assert blocks[0] == {"kind": "reasoning", "text": "The user said hi"}
    assert blocks[1] == {"kind": "text", "text": "Hello World"}
    assert blocks[2]["kind"] == "tool"
    assert blocks[2]["tool"] == "read"
    assert blocks[2]["result"] == "file content..."


def test_old_format_without_tools_migrates_cleanly(
    tmp_path: Path,
) -> None:
    """Old-format assistant_turn with only content (no reasoning/tools) migrates."""
    store = JSONLTranscriptStore(tmp_path)
    old_event = {
        "event": "assistant_turn",
        "conversation_id": "conv2",
        "agent_name": "main",
        "timestamp": 1718234567.0,
        "content": "Just text, nothing else",
        "turn_id": "turn_1",
        "latency_ms": 500,
    }
    conv_dir = tmp_path / "conv2"
    conv_dir.mkdir()
    (conv_dir / "main.jsonl").write_text(
        json.dumps(old_event, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    loaded = list(store.load("conv2", "main"))
    assert len(loaded) == 1
    blocks = loaded[0].blocks  # type: ignore[attr-defined]
    assert len(blocks) == 1
    assert blocks[0] == {"kind": "text", "text": "Just text, nothing else"}


# ── load_all (multi-agent merge) ────────────────────────────────────────


@pytest.mark.parametrize("store_factory", [_make_jsonl_store, _make_sqlite_store])
def test_load_all_merges_agents_by_timestamp(
    store_factory: object,
) -> None:
    """load_all merges events from all agents, sorted by timestamp."""
    store: TranscriptStore = store_factory()  # type: ignore[operator]
    # main agent: user msg at t=100, assistant at t=200
    store.append("conv1", "main", UserMessageEvent(
        conversation_id="conv1", agent_name="main", content="hi",
        timestamp=100.0,
    ))
    store.append("conv1", "main", AssistantTurnEvent(
        conversation_id="conv1", agent_name="main",
        blocks=[{"kind": "text", "text": "hello"}],
        turn_id="t1", latency_ms=500, timestamp=200.0,
    ))
    # coding agent: assistant at t=150 (between the two main events)
    store.append("conv1", "coding", AssistantTurnEvent(
        conversation_id="conv1", agent_name="coding",
        blocks=[{"kind": "text", "text": "code output"}],
        turn_id="t1", latency_ms=300, timestamp=150.0,
    ))

    all_events = list(store.load_all("conv1"))
    assert len(all_events) == 3
    # Order: main user (t=100), coding assistant (t=150), main assistant (t=200)
    assert all_events[0].event == "user_message"
    assert all_events[1].event == "assistant_turn"
    assert all_events[1].agent_name == "coding"  # type: ignore[attr-defined]
    assert all_events[2].event == "assistant_turn"
    assert all_events[2].agent_name == "main"  # type: ignore[attr-defined]


@pytest.mark.parametrize("store_factory", [_make_jsonl_store, _make_sqlite_store])
def test_load_all_empty_conversation(
    store_factory: object,
) -> None:
    """load_all returns nothing for nonexistent conversation."""
    store: TranscriptStore = store_factory()  # type: ignore[operator]
    assert list(store.load_all("nonexistent")) == []