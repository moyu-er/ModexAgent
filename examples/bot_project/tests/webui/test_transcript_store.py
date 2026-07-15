"""Tests for the session_id-keyed TranscriptStore (JSONL)."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from bot.webui.events import (
    AssistantTextEvent,
    AssistantTurnEvent,
    ModelContentDelta,
    ServerEvent,
    ToolCallEvent,
    ToolResultEvent,
    TurnStartEvent,
    UserMessageEvent,
)
from bot.webui.transcript_store import (
    JSONLTranscriptStore,
    MaterializedTurn,
    ResilientTranscriptStore,
    TranscriptStore,
)

pytestmark = pytest.mark.asyncio


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


async def test_append_and_load_events() -> None:
    store: TranscriptStore = _make_store()
    await store.append("abc.main", _msg("abc.main"))
    await store.append(
        "abc.main",
        ModelContentDelta(session_id="abc.main", agent_name="main", text="hello", turn_id="t1"),
    )
    events = await store.load("abc.main")
    assert len(events) == 2
    assert events[0].event == "user_message"
    assert events[1].event == "model_content_delta"


async def test_load_empty_session_returns_nothing() -> None:
    store: TranscriptStore = _make_store()
    assert await store.load("nonexistent.main") == []


async def test_two_subagent_invocations_persist_to_separate_sessions() -> None:
    """Regression: two reviewer invocations must NOT collapse into one file.

    The real session_id carries an invocation_id segment
    (``{conv}.{agent}.{invocation_id}``). The store must key by the FULL
    session_id so each invocation is independently persisted and loadable.
    """
    store: TranscriptStore = _make_store()
    await store.append("conv.reviewer.aa11", _msg("conv.reviewer.aa11", "review 1"))
    await store.append("conv.reviewer.bb22", _msg("conv.reviewer.bb22", "review 2"))

    first = await store.load("conv.reviewer.aa11")
    second = await store.load("conv.reviewer.bb22")
    assert len(first) == 1
    assert len(second) == 1
    assert first[0].content == "review 1"  # type: ignore[attr-defined]
    assert second[0].content == "review 2"  # type: ignore[attr-defined]


# ── Listing ────────────────────────────────────────────────────────────────


async def test_list_sessions_returns_full_session_ids() -> None:
    store: TranscriptStore = _make_store()
    await store.append("abc.main", _msg("abc.main", agent_name="main"))
    await store.append("abc.office-expert", _msg("abc.office-expert", agent_name="office-expert"))
    sessions = await store.list_sessions()
    assert sessions == {"abc.main", "abc.office-expert"}


async def test_list_sessions_by_prefix_groups_by_prefix() -> None:
    store: TranscriptStore = _make_store()
    await store.append("abc.main", _msg("abc.main"))
    await store.append("abc.reviewer.zz99", _msg("abc.reviewer.zz99"))
    await store.append("xyz.main", _msg("xyz.main"))
    sessions = await store.list_sessions_by_prefix("abc")
    assert sessions == {"abc.main", "abc.reviewer.zz99"}


# ── load_sessions_by_prefix (merge across sessions by timestamp) ─────────────────


async def test_load_sessions_by_prefix_merges_sessions_by_timestamp() -> None:
    store: TranscriptStore = _make_store()
    await store.append("conv.main", _msg("conv.main", "hi", timestamp=100.0))
    await store.append(
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
    await store.append(
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

    all_events = await store.load_sessions_by_prefix("conv")
    assert len(all_events) == 3
    assert all_events[0].event == "user_message"
    assert all_events[1].agent_name == "reviewer"  # t=150
    assert all_events[2].agent_name == "main"  # t=200


async def test_load_sessions_by_prefix_empty_returns_nothing() -> None:
    store: TranscriptStore = _make_store()
    assert await store.load_sessions_by_prefix("nonexistent") == []


# ── Delete ─────────────────────────────────────────────────────────────────


async def test_delete_session_removes_only_that_session() -> None:
    store: TranscriptStore = _make_store()
    await store.append("abc.main", _msg("abc.main"))
    await store.append("abc.reviewer.aa", _msg("abc.reviewer.aa"))
    await store.delete_session("abc.main")
    assert await store.load("abc.main") == []
    assert len(await store.load("abc.reviewer.aa")) == 1


async def test_delete_sessions_by_prefix_removes_all_sessions_in_conversation() -> None:
    store: TranscriptStore = _make_store()
    await store.append("abc.main", _msg("abc.main"))
    await store.append("abc.reviewer.aa", _msg("abc.reviewer.aa"))
    await store.append("xyz.main", _msg("xyz.main"))
    await store.delete_sessions_by_prefix("abc")
    assert await store.list_sessions_by_prefix("abc") == set()
    assert "xyz.main" in await store.list_sessions()


# ── Round-trip & migration ─────────────────────────────────────────────────


async def test_assistant_turn_roundtrip() -> None:
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
    await store.append("conv1.main", ev)
    loaded = await store.load("conv1.main")
    assert len(loaded) == 1
    assert loaded[0].event == "assistant_turn"
    blocks = loaded[0].blocks  # type: ignore[attr-defined]
    assert len(blocks) == 3
    assert blocks[2] == {"kind": "tool", "tool": "read", "args": {"path": "x"}, "result": "ok"}


async def test_old_format_assistant_turn_migrated_on_load(tmp_path: Path) -> None:
    """Old-format assistant_turn (content/reasoning/tools) migrates to blocks."""
    store = JSONLTranscriptStore(tmp_path)
    old_event = {
        "event": "assistant_turn",
        "session_id": "conv1",
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

    loaded = await store.load("conv1.main")
    assert len(loaded) == 1
    blocks = loaded[0].blocks  # type: ignore[attr-defined]
    assert len(blocks) == 3
    assert blocks[0] == {"kind": "reasoning", "text": "The user said hi"}
    assert blocks[1] == {"kind": "text", "text": "Hello World"}
    assert blocks[2]["tool"] == "read"


# ── load_materialized_by_prefix ─────────────────────────────────────────


async def test_materialize_single_text_turn() -> None:
    store = _make_store()
    await store.append("conv.main", TurnStartEvent(
        session_id="conv.main", agent_name="main", turn_id="t1", timestamp=100.0))
    await store.append("conv.main", AssistantTextEvent(
        session_id="conv.main", agent_name="main", turn_id="t1", text="Hello", timestamp=200.0))
    turns = await store.load_materialized_by_prefix("conv")
    assert len(turns) == 1
    assert turns[0].turn_id == "t1"
    assert turns[0].blocks == [{"kind": "text", "text": "Hello"}]


async def test_materialize_text_and_tool_turn() -> None:
    store = _make_store()
    await store.append("conv.main", TurnStartEvent(
        session_id="conv.main", agent_name="main", turn_id="t1", timestamp=100.0))
    await store.append("conv.main", AssistantTextEvent(
        session_id="conv.main", agent_name="main", turn_id="t1", text="Let me check.", timestamp=200.0))
    await store.append("conv.main", ToolCallEvent(
        session_id="conv.main", agent_name="main", turn_id="t1", call_id="call_0",
        tool_name="read_file", args={"path": "/x"}, timestamp=300.0))
    await store.append("conv.main", ToolResultEvent(
        session_id="conv.main", agent_name="main", turn_id="t1", call_id="call_0",
        tool_name="read_file", result="content", timestamp=400.0))
    turns = await store.load_materialized_by_prefix("conv")
    assert len(turns) == 1
    blocks = turns[0].blocks
    assert len(blocks) == 2
    assert blocks[0] == {"kind": "text", "text": "Let me check."}
    assert blocks[1] == {"kind": "tool", "tool": "read_file", "args": {"path": "/x"}, "result": "content"}


async def test_materialize_multiple_turns_sorted() -> None:
    store = _make_store()
    await store.append("conv.main", TurnStartEvent(
        session_id="conv.main", agent_name="main", turn_id="t2", timestamp=300.0))
    await store.append("conv.main", AssistantTextEvent(
        session_id="conv.main", agent_name="main", turn_id="t2", text="Second", timestamp=400.0))
    await store.append("conv.main", TurnStartEvent(
        session_id="conv.main", agent_name="main", turn_id="t1", timestamp=100.0))
    await store.append("conv.main", AssistantTextEvent(
        session_id="conv.main", agent_name="main", turn_id="t1", text="First", timestamp=200.0))
    turns = await store.load_materialized_by_prefix("conv")
    assert len(turns) == 2
    assert turns[0].turn_id == "t1"
    assert turns[1].turn_id == "t2"


async def test_materialize_tool_call_with_error_result() -> None:
    store = _make_store()
    await store.append("conv.main", TurnStartEvent(
        session_id="conv.main", agent_name="main", turn_id="t1", timestamp=100.0))
    await store.append("conv.main", ToolCallEvent(
        session_id="conv.main", agent_name="main", turn_id="t1", call_id="c0",
        tool_name="rm", args={"path": "/x"}, timestamp=200.0))
    await store.append("conv.main", ToolResultEvent(
        session_id="conv.main", agent_name="main", turn_id="t1", call_id="c0",
        tool_name="rm", result="", error="Permission denied", timestamp=300.0))
    turns = await store.load_materialized_by_prefix("conv")
    assert turns[0].blocks[0]["result"] == "Error: Permission denied"


async def test_materialize_legacy_assistant_turn_falls_through() -> None:
    store = _make_store()
    await store.append("conv.main", AssistantTurnEvent(
        session_id="conv.main", agent_name="main", turn_id="t1",
        blocks=[{"kind": "reasoning", "text": "think"}, {"kind": "text", "text": "reply"}],
        latency_ms=500, timestamp=100.0))
    turns = await store.load_materialized_by_prefix("conv")
    assert len(turns) == 1
    assert len(turns[0].blocks) == 2


async def test_materialize_empty_returns_empty() -> None:
    store = _make_store()
    assert await store.load_materialized_by_prefix("nonexistent") == []


async def test_materialize_tool_call_without_result_not_in_blocks() -> None:
    store = _make_store()
    await store.append("conv.main", TurnStartEvent(
        session_id="conv.main", agent_name="main", turn_id="t1", timestamp=100.0))
    await store.append("conv.main", ToolCallEvent(
        session_id="conv.main", agent_name="main", turn_id="t1", call_id="orphan",
        tool_name="search", args={"q": "x"}, timestamp=200.0))
    turns = await store.load_materialized_by_prefix("conv")
    assert turns[0].blocks == []


async def test_materialize_assistant_turn_carries_attachments() -> None:
    """An AssistantTurnEvent with turn_id and attachments contributes its
    attachments to the materialized turn so history replay can re-render
    download cards after a refresh (ADR-0013 §11).
    """
    store = _make_store()
    await store.append("conv.main", TurnStartEvent(
        session_id="conv.main", agent_name="main", turn_id="t1", timestamp=100.0))
    await store.append("conv.main", AssistantTextEvent(
        session_id="conv.main", agent_name="main", turn_id="t1",
        text="here is the file", timestamp=200.0))
    await store.append("conv.main", AssistantTurnEvent(
        session_id="conv.main", agent_name="main", turn_id="t1",
        blocks=[{"kind": "text", "text": "done"}],
        attachments=[{"id": "att-1", "kind": "other", "name": "report.txt",
                       "mime": "text/plain", "size": 4, "path": "/x/report.txt",
                       "locator": "workspace"}],
        timestamp=300.0))
    turns = await store.load_materialized_by_prefix("conv")
    assert len(turns) == 1
    assert turns[0].turn_id == "t1"
    assert len(turns[0].attachments) == 1
    assert turns[0].attachments[0]["id"] == "att-1"
    assert turns[0].attachments[0]["name"] == "report.txt"


async def test_materialize_attachment_only_event_without_turn_id_emits_standalone_turn() -> None:
    """A SendFileToUserTool-persisted AssistantTurnEvent has no turn_id and
    empty blocks (it only carries the outbound Attachment record). It must be
    emitted as its own MaterializedTurn with empty blocks and the attachment
    list, so the history-replay API returns it for the frontend to render a
    download card after refresh.
    """
    store = _make_store()
    await store.append("conv.main", _msg("conv.main", "hi", timestamp=100.0))
    await store.append("conv.main", AssistantTurnEvent(
        session_id="conv.main", agent_name="main",
        blocks=[],
        attachments=[{"id": "att-out", "kind": "image", "name": "chart.png",
                       "mime": "image/png", "size": 11,
                       "path": "/ws/chart.png", "locator": "workspace"}],
        timestamp=200.0))
    turns = await store.load_materialized_by_prefix("conv")
    assert len(turns) == 1
    assert turns[0].turn_id == ""
    assert turns[0].blocks == []
    assert len(turns[0].attachments) == 1
    assert turns[0].attachments[0]["id"] == "att-out"
    # ServerEvent.from_dict migrates float seconds -> int milliseconds on load.
    assert turns[0].started_at == 200_000


async def test_materialize_mixed_real_turn_and_attachment_only_turn_sorted() -> None:
    """A real turn (with turn_id) and a standalone attachment carrier (no
    turn_id) both materialize and stay ordered by timestamp.
    """
    store = _make_store()
    await store.append("conv.main", TurnStartEvent(
        session_id="conv.main", agent_name="main", turn_id="t1", timestamp=100.0))
    await store.append("conv.main", AssistantTextEvent(
        session_id="conv.main", agent_name="main", turn_id="t1",
        text="hi", timestamp=150.0))
    await store.append("conv.main", AssistantTurnEvent(
        session_id="conv.main", agent_name="main",
        blocks=[],
        attachments=[{"id": "att-mid", "kind": "other", "name": "data.csv",
                       "mime": "text/csv", "size": 5, "path": "/x/data.csv",
                       "locator": "workspace"}],
        timestamp=200.0))
    turns = await store.load_materialized_by_prefix("conv")
    assert len(turns) == 2
    assert turns[0].turn_id == "t1"
    assert turns[0].blocks == [{"kind": "text", "text": "hi"}]
    assert turns[0].attachments == []
    assert turns[1].turn_id == ""
    assert turns[1].blocks == []
    assert len(turns[1].attachments) == 1
    assert turns[1].attachments[0]["id"] == "att-mid"


# ── ResilientTranscriptStore (I/O resilience) ───────────────────────────────


class _FlakyDelegate(TranscriptStore):
    """In-memory delegate whose ``append`` fails on demand."""

    def __init__(self) -> None:
        self.events: list[tuple[str, ServerEvent]] = []
        self.fail_next: bool = False

    async def append(
        self, session_id: str, event: ServerEvent, *, pool: str = "main"
    ) -> None:
        del pool
        if self.fail_next:
            self.fail_next = False
            raise OSError("simulated disk full")
        self.events.append((session_id, event))

    async def load(self, session_id: str) -> list[ServerEvent]:
        return [evt for sid, evt in self.events if sid == session_id]

    async def load_sessions_by_prefix(
        self, session_prefix: str, *, pool: str | None = None
    ) -> list[ServerEvent]:
        del pool
        return [
            evt
            for sid, evt in self.events
            if sid.split(".", 1)[0] == session_prefix
        ]

    async def list_sessions(self) -> set[str]:
        return {sid for sid, _ in self.events}

    async def list_sessions_by_prefix(self, session_prefix: str) -> set[str]:
        return {
            sid
            for sid in await self.list_sessions()
            if sid.split(".", 1)[0] == session_prefix
        }

    async def delete_session(self, session_id: str) -> None:
        self.events = [(s, e) for s, e in self.events if s != session_id]

    async def delete_sessions_by_prefix(self, session_prefix: str) -> None:
        self.events = [(s, e) for s, e in self.events if s.split(".", 1)[0] != session_prefix]


async def test_resilient_append_swallows_io_error() -> None:
    """An OSError during append must not propagate to the agent run."""
    delegate = _FlakyDelegate()
    store: TranscriptStore = ResilientTranscriptStore(delegate)

    delegate.fail_next = True
    # Must NOT raise — agent turn keeps going despite the disk failure.
    await store.append("conv.main", _msg("conv.main"))


async def test_resilient_append_recovers_after_failure() -> None:
    """After a swallowed failure, subsequent writes still land."""
    delegate = _FlakyDelegate()
    store: TranscriptStore = ResilientTranscriptStore(delegate)

    delegate.fail_next = True
    await store.append("conv.main", _msg("conv.main", "lost"))
    await store.append("conv.main", _msg("conv.main", "kept"))

    assert [e for _, e in delegate.events]  # the recovery write landed
    assert delegate.events[0][1].content == "kept"  # type: ignore[attr-defined]


async def test_resilient_delegates_read_paths() -> None:
    """Read/list/delete pass through to the delegate unchanged."""
    delegate = _FlakyDelegate()
    store: TranscriptStore = ResilientTranscriptStore(delegate)

    await store.append("conv.main", _msg("conv.main", "hi"))
    await store.append("conv.reviewer.aa", _msg("conv.reviewer.aa", "review"))

    assert await store.list_sessions() == {"conv.main", "conv.reviewer.aa"}
    assert await store.list_sessions_by_prefix("conv") == {"conv.main", "conv.reviewer.aa"}
    assert len(await store.load("conv.main")) == 1

    await store.delete_session("conv.main")
    assert await store.list_sessions() == {"conv.reviewer.aa"}


async def test_workspace_store_append_is_resilient(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The production wiring routes physical writes through the resilient wrapper.

    A disk error surfacing from the underlying JSONL store must be swallowed at
    the WorkspaceScopedTranscriptStore level so neither the emitter nor the S7
    user-message persist stage can crash an agent turn.
    """
    from bot.service.workspace_store import WorkspaceScopedTranscriptStore
    from modex_agent.workspace.runtime import bind_workspace_root

    def _workspace() -> str:
        return "ws"

    store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")

    async def _boom(
        self: object, session_id: str, event: object, *, pool: str = "main"
    ) -> None:
        del pool
        raise OSError("disk full")

    monkeypatch.setattr(JSONLTranscriptStore, "append", _boom)

    # Must NOT raise — proving the resilient wrapper sits in the write path.
    with bind_workspace_root(tmp_path):
        await store.append("conv.main", _msg("conv.main", "hi"))


