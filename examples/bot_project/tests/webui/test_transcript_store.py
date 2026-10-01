"""Tests for the session_id-keyed TranscriptStore (JSONL)."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest
from bot.webui.transcript_store import (
    AttachmentCarrier,
    JSONLTranscriptStore,
    ResilientTranscriptStore,
    TranscriptRecord,
    TranscriptStore,
    UserMessageRecord,
)

from modex_agent.core.turn_events import StopReason
from modex_agent.presentation import (
    TextDelta,
    ToolCallStarted,
    ToolResult,
    TurnFinished,
    TurnStarted,
)

pytestmark = pytest.mark.asyncio


# ── Helpers ────────────────────────────────────────────────────────────────


def _make_store() -> JSONLTranscriptStore:
    return JSONLTranscriptStore(Path(tempfile.mkdtemp()))


def _msg(session_id: str, content: str = "hi", **kwargs: object) -> UserMessageRecord:
    return UserMessageRecord(
        session_id=session_id,
        agent_name=str(kwargs.get("agent_name", "main")),
        timestamp_ms=int(kwargs.get("timestamp", 100)),
        content=content,
    )


# ── Core: append / load keyed by full session_id ──────────────────────────


async def test_append_and_load_events() -> None:
    store: TranscriptStore = _make_store()
    await store.append("abc.main", _msg("abc.main"))
    await store.append(
        "abc.main",
        TextDelta(
            session_id="abc.main",
            agent_name="main",
            turn_id="t1",
            timestamp_ms=101,
            text="hello",
        ),
    )
    records = await store.load("abc.main")
    assert len(records) == 2
    assert records[0].kind == "user_message"
    assert records[1].kind == "text_delta"


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
    assert first[0].content == "review 1"
    assert second[0].content == "review 2"


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
    await store.append("conv.main", _msg("conv.main", "hi", timestamp=100))
    await store.append(
        "conv.main",
        TurnFinished(
            session_id="conv.main",
            agent_name="main",
            turn_id="t1",
            timestamp_ms=200,
            stop_reason=StopReason.COMPLETED,
        ),
    )
    await store.append(
        "conv.reviewer.aa",
        TextDelta(
            session_id="conv.reviewer.aa",
            agent_name="reviewer",
            turn_id="t1",
            timestamp_ms=150,
            text="review",
            segment_id="_legacy_a",
        ),
    )

    all_records = await store.load_sessions_by_prefix("conv")
    assert len(all_records) == 3
    assert all_records[0].kind == "user_message"
    assert all_records[1].agent_name == "reviewer"  # t=150
    assert all_records[2].agent_name == "main"  # t=200


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


# ── Round-trip & legacy generation detection ────────────────────────────────


async def test_legacy_server_event_lines_replay_through_the_same_store() -> None:
    """A pre-cutover JSONL file (``event`` discriminator lines) loads and
    replays through the same store as new ``kind`` records — read
    compatibility with no on-disk migration."""
    base_dir = Path(tempfile.mkdtemp())
    store = JSONLTranscriptStore(base_dir)
    legacy_user = {
        "event": "user_message",
        "session_id": "conv1.main",
        "agent_name": "main",
        "timestamp": 1718234567.0,
        "content": "hello",
    }
    legacy_turn = {
        "event": "assistant_turn",
        "session_id": "conv1.main",
        "agent_name": "main",
        "timestamp": 1718234568.0,
        "blocks": [
            {"kind": "reasoning", "text": "The user said hi"},
            {"kind": "text", "text": "Hello"},
            {"kind": "tool", "tool": "read", "args": {"path": "x"}, "result": "ok"},
        ],
        "turn_id": "turn_1",
        "latency_ms": 500,
        "attachments": [
            {"id": "att-1", "kind": "other", "name": "report.txt",
             "mime": "text/plain", "size": 4, "path": "/x/report.txt",
             "locator": "workspace"}
        ],
    }
    # Written exactly as the pre-cutover writer serialized them.
    (base_dir / "conv1.main.jsonl").write_text(
        json.dumps(legacy_user, ensure_ascii=False) + "\n"
        + json.dumps(legacy_turn, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    records = await store.load("conv1.main")
    kinds = [record.kind for record in records]
    assert kinds == [
        "user_message",
        "thinking_delta",
        "text_delta",
        "tool_call_started",
        "tool_result",
        "attachments",
    ]

    turns = await store.load_materialized_by_prefix("conv1")
    assert len(turns) == 1
    assert turns[0].turn_id == "turn_1"
    assert turns[0].blocks == [
        {"kind": "reasoning", "text": "The user said hi"},
        {"kind": "text", "text": "Hello"},
        {"kind": "tool", "tool": "read", "args": {"path": "x"}, "result": "ok"},
    ]
    # The legacy turn's attachment records ride a carrier attached to the
    # same turn id.
    assert turns[0].attachments[0]["id"] == "att-1"
    # Float seconds -> int milliseconds at the legacy decode boundary.
    assert turns[0].started_at == 1_718_234_568_000


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

    turns = await store.load_materialized_by_prefix("conv1")
    assert len(turns) == 1
    blocks = turns[0].blocks
    assert len(blocks) == 3
    assert blocks[0] == {"kind": "reasoning", "text": "The user said hi"}
    assert blocks[1] == {"kind": "text", "text": "Hello World"}
    assert blocks[2]["tool"] == "read"


async def test_mixed_generation_file_replays_in_one_pass(tmp_path: Path) -> None:
    """Old and new lines may interleave in one file (a session that spans
    the cutover); both replay through the single materializer."""
    store = JSONLTranscriptStore(tmp_path)
    legacy_text = {
        "event": "assistant_text",
        "session_id": "conv1.main",
        "agent_name": "main",
        "timestamp": 100,
        "turn_id": "t1",
        "text": "legacy block",
    }
    (tmp_path / "conv1.main.jsonl").write_text(
        json.dumps(legacy_text, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    await store.append(
        "conv1.main",
        TextDelta(
            session_id="conv1.main",
            agent_name="main",
            turn_id="t1",
            timestamp_ms=200,
            text="new block",
            segment_id="_text",
        ),
    )
    turns = await store.load_materialized_by_prefix("conv1")
    assert len(turns) == 1
    assert turns[0].blocks == [
        {"kind": "text", "text": "legacy block"},
        {"kind": "text", "text": "new block"},
    ]


# ── load_materialized_by_prefix ─────────────────────────────────────────


async def test_materialize_single_text_turn() -> None:
    store = _make_store()
    await store.append("conv.main", TurnStarted(
        session_id="conv.main", agent_name="main", turn_id="t1", timestamp_ms=100))
    await store.append("conv.main", TextDelta(
        session_id="conv.main", agent_name="main", turn_id="t1",
        timestamp_ms=200, text="Hello", segment_id="_text"))
    turns = await store.load_materialized_by_prefix("conv")
    assert len(turns) == 1
    assert turns[0].turn_id == "t1"
    assert turns[0].blocks == [{"kind": "text", "text": "Hello"}]


async def test_materialize_text_and_tool_turn() -> None:
    store = _make_store()
    await store.append("conv.main", TurnStarted(
        session_id="conv.main", agent_name="main", turn_id="t1", timestamp_ms=100))
    await store.append("conv.main", TextDelta(
        session_id="conv.main", agent_name="main", turn_id="t1",
        text="Let me check.", timestamp_ms=200, segment_id="_text"))
    await store.append("conv.main", ToolCallStarted(
        session_id="conv.main", agent_name="main", turn_id="t1", call_id="call_0",
        tool_name="read_file", arguments={"path": "/x"}, timestamp_ms=300))
    await store.append("conv.main", ToolResult(
        session_id="conv.main", agent_name="main", turn_id="t1", call_id="call_0",
        tool_name="read_file", output="content", timestamp_ms=400))
    turns = await store.load_materialized_by_prefix("conv")
    assert len(turns) == 1
    blocks = turns[0].blocks
    assert len(blocks) == 2
    assert blocks[0] == {"kind": "text", "text": "Let me check."}
    assert blocks[1] == {"kind": "tool", "tool": "read_file", "args": {"path": "/x"}, "result": "content"}


async def test_materialize_single_batch_tools_in_model_sequence() -> None:
    store = _make_store()
    for call_id, seq, timestamp in [("B", 1, 100), ("A", 0, 200)]:
        await store.append("conv.main", ToolCallStarted(
            session_id="conv.main", agent_name="main", turn_id="t1",
            call_id=call_id, tool_name=call_id, arguments={}, timestamp_ms=timestamp))
        await store.append("conv.main", ToolResult(
            session_id="conv.main", agent_name="main", turn_id="t1",
            call_id=call_id, tool_name=call_id, output=call_id,
            seq=seq, timestamp_ms=timestamp + 1))

    turns = await store.load_materialized_by_prefix("conv")

    assert [block["tool"] for block in turns[0].blocks] == ["A", "B"]


async def test_materialize_two_batches_in_turn_wide_model_sequence() -> None:
    store = _make_store()
    completed = [
        ("batch1-B", 1, 100),
        ("batch1-A", 0, 200),
        ("batch2-D", 3, 300),
        ("batch2-C", 2, 400),
    ]
    for call_id, seq, timestamp in completed:
        await store.append("conv.main", ToolCallStarted(
            session_id="conv.main", agent_name="main", turn_id="t1",
            call_id=call_id, tool_name=call_id, arguments={}, timestamp_ms=timestamp))
        await store.append("conv.main", ToolResult(
            session_id="conv.main", agent_name="main", turn_id="t1",
            call_id=call_id, tool_name=call_id, output=call_id,
            seq=seq, timestamp_ms=timestamp + 1))

    turns = await store.load_materialized_by_prefix("conv")

    assert [block["tool"] for block in turns[0].blocks] == [
        "batch1-A",
        "batch1-B",
        "batch2-C",
        "batch2-D",
    ]


async def test_materialize_legacy_tool_result_stays_in_timestamp_slot() -> None:
    store = _make_store()
    completed = [("B", 1, 100), ("legacy", None, 200), ("A", 0, 300)]
    for call_id, seq, timestamp in completed:
        await store.append("conv.main", ToolCallStarted(
            session_id="conv.main", agent_name="main", turn_id="t1",
            call_id=call_id, tool_name=call_id, arguments={}, timestamp_ms=timestamp))
        await store.append("conv.main", ToolResult(
            session_id="conv.main", agent_name="main", turn_id="t1",
            call_id=call_id, tool_name=call_id, output=call_id,
            seq=seq, timestamp_ms=timestamp + 1))

    turns = await store.load_materialized_by_prefix("conv")

    assert [block["tool"] for block in turns[0].blocks] == ["A", "legacy", "B"]


async def test_materialize_multiple_turns_sorted() -> None:
    store = _make_store()
    await store.append("conv.main", TurnStarted(
        session_id="conv.main", agent_name="main", turn_id="t2", timestamp_ms=300))
    await store.append("conv.main", TextDelta(
        session_id="conv.main", agent_name="main", turn_id="t2",
        text="Second", timestamp_ms=400, segment_id="_text"))
    await store.append("conv.main", TurnStarted(
        session_id="conv.main", agent_name="main", turn_id="t1", timestamp_ms=100))
    await store.append("conv.main", TextDelta(
        session_id="conv.main", agent_name="main", turn_id="t1",
        text="First", timestamp_ms=200, segment_id="_text"))
    turns = await store.load_materialized_by_prefix("conv")
    assert len(turns) == 2
    assert turns[0].turn_id == "t1"
    assert turns[1].turn_id == "t2"


async def test_materialize_tool_call_with_error_result() -> None:
    store = _make_store()
    await store.append("conv.main", TurnStarted(
        session_id="conv.main", agent_name="main", turn_id="t1", timestamp_ms=100))
    await store.append("conv.main", ToolCallStarted(
        session_id="conv.main", agent_name="main", turn_id="t1", call_id="c0",
        tool_name="rm", arguments={"path": "/x"}, timestamp_ms=200))
    await store.append("conv.main", ToolResult(
        session_id="conv.main", agent_name="main", turn_id="t1", call_id="c0",
        tool_name="rm", output="", error="Permission denied", timestamp_ms=300))
    turns = await store.load_materialized_by_prefix("conv")
    assert turns[0].blocks[0]["result"] == "Error: Permission denied"


async def test_materialize_orphan_tool_result_without_call() -> None:
    """A resumed approval turn persists ONLY the result record (no started
    card): the card materializes at the result with empty args — the
    orphan-result shape the resume path writes."""
    store = _make_store()
    await store.append("conv.main", ToolResult(
        session_id="conv.main", agent_name="main", turn_id="t1", call_id="c0",
        tool_name="search", output="hits", timestamp_ms=100))
    turns = await store.load_materialized_by_prefix("conv")
    assert turns[0].blocks == [
        {"kind": "tool", "tool": "search", "args": {}, "result": "hits"}
    ]


async def test_materialize_empty_returns_empty() -> None:
    store = _make_store()
    assert await store.load_materialized_by_prefix("nonexistent") == []


async def test_materialize_assistant_turn_carries_attachments() -> None:
    """An AttachmentCarrier with a turn_id attaches its records to that
    turn so history replay can re-render download cards after a refresh
    (ADR-0013 §11) — the replay shape of legacy assistant_turn records
    that carried attachments.
    """
    store = _make_store()
    await store.append("conv.main", TurnStarted(
        session_id="conv.main", agent_name="main", turn_id="t1", timestamp_ms=100))
    await store.append("conv.main", TextDelta(
        session_id="conv.main", agent_name="main", turn_id="t1",
        text="here is the file", timestamp_ms=200, segment_id="_text"))
    await store.append("conv.main", AttachmentCarrier(
        session_id="conv.main", agent_name="main", turn_id="t1", timestamp_ms=300,
        attachments=[{"id": "att-1", "kind": "other", "name": "report.txt",
                      "mime": "text/plain", "size": 4, "path": "/x/report.txt",
                      "locator": "workspace"}]))
    turns = await store.load_materialized_by_prefix("conv")
    assert len(turns) == 1
    assert turns[0].turn_id == "t1"
    assert len(turns[0].attachments) == 1
    assert turns[0].attachments[0]["id"] == "att-1"
    assert turns[0].attachments[0]["name"] == "report.txt"


async def test_materialize_attachment_only_carrier_emits_standalone_turn() -> None:
    """A SendFileToUserTool-persisted carrier has no turn_id (it only
    carries the outbound Attachment record). It must be emitted as its own
    MaterializedTurn with empty blocks and the attachment list, so the
    history-replay API returns it for the frontend to render a download
    card after refresh.
    """
    store = _make_store()
    await store.append("conv.main", _msg("conv.main", "hi", timestamp=100))
    await store.append("conv.main", AttachmentCarrier(
        session_id="conv.main", agent_name="main", timestamp_ms=200,
        attachments=[{"id": "att-out", "kind": "image", "name": "chart.png",
                      "mime": "image/png", "size": 11,
                      "path": "/ws/chart.png", "locator": "workspace"}]))
    turns = await store.load_materialized_by_prefix("conv")
    assert len(turns) == 1
    assert turns[0].turn_id == ""
    assert turns[0].blocks == []
    assert len(turns[0].attachments) == 1
    assert turns[0].attachments[0]["id"] == "att-out"
    assert turns[0].started_at == 200


async def test_materialize_mixed_real_turn_and_attachment_only_turn_sorted() -> None:
    """A real turn (with turn_id) and a standalone attachment carrier (no
    turn_id) both materialize and stay ordered by timestamp.
    """
    store = _make_store()
    await store.append("conv.main", TurnStarted(
        session_id="conv.main", agent_name="main", turn_id="t1", timestamp_ms=100))
    await store.append("conv.main", TextDelta(
        session_id="conv.main", agent_name="main", turn_id="t1",
        text="hi", timestamp_ms=150, segment_id="_text"))
    await store.append("conv.main", AttachmentCarrier(
        session_id="conv.main", agent_name="main", timestamp_ms=200,
        attachments=[{"id": "att-mid", "kind": "other", "name": "data.csv",
                      "mime": "text/csv", "size": 5, "path": "/x/data.csv",
                      "locator": "workspace"}]))
    turns = await store.load_materialized_by_prefix("conv")
    assert len(turns) == 2
    assert turns[0].turn_id == "t1"
    assert turns[0].blocks == [{"kind": "text", "text": "hi"}]
    assert turns[0].attachments == []
    assert turns[1].turn_id == ""
    assert turns[1].blocks == []
    assert len(turns[1].attachments) == 1
    assert turns[1].attachments[0]["id"] == "att-mid"


async def test_materialize_interleaved_subagent_records_keep_one_main_turn() -> None:
    """A main turn whose records straddle a subagent's records (the prefix
    merge interleaves main + subagent session files by timestamp) must
    replay as ONE materialized turn — not fragment into one MaterializedTurn
    per contiguity group sharing the turn id (the refresh-fragmentation
    regression).
    """
    store = _make_store()
    # Main turn opens and streams its first segment...
    await store.append("conv.main", TurnStarted(
        session_id="conv.main", agent_name="main", turn_id="t-main", timestamp_ms=100))
    await store.append("conv.main", TextDelta(
        session_id="conv.main", agent_name="main", turn_id="t-main",
        text="part one", timestamp_ms=110, segment_id="_text"))
    # ...the subagent it dispatched mid-flight runs in its own session...
    await store.append("conv.researcher.inv1", TextDelta(
        session_id="conv.researcher.inv1", agent_name="researcher", turn_id="t-sub",
        text="sub finding", timestamp_ms=120, segment_id="_text"))
    # ...then the main turn continues (tool + closing text) around it.
    await store.append("conv.main", ToolCallStarted(
        session_id="conv.main", agent_name="main", turn_id="t-main", call_id="c1",
        tool_name="search", arguments={"q": "x"}, timestamp_ms=130))
    await store.append("conv.main", ToolResult(
        session_id="conv.main", agent_name="main", turn_id="t-main", call_id="c1",
        tool_name="search", output="hits", timestamp_ms=140))
    await store.append("conv.researcher.inv1", TextDelta(
        session_id="conv.researcher.inv1", agent_name="researcher", turn_id="t-sub",
        text="sub done", timestamp_ms=150, segment_id="_text"))
    await store.append("conv.main", TextDelta(
        session_id="conv.main", agent_name="main", turn_id="t-main",
        text="part two", timestamp_ms=160, segment_id="_text"))

    # The prefix merge is the composition the history API reads through.
    turns = await store.load_materialized_by_prefix("conv")

    main_turns = [turn for turn in turns if turn.turn_id == "t-main"]
    assert len(main_turns) == 1, (
        f"interleaved main turn fragmented into {len(main_turns)} turns: "
        f"{[t.blocks for t in turns]}"
    )
    # Fragment blocks concatenate in chronology: streamed text, tool, tail.
    assert main_turns[0].blocks == [
        {"kind": "text", "text": "part one"},
        {"kind": "tool", "tool": "search", "args": {"q": "x"}, "result": "hits"},
        {"kind": "text", "text": "part two"},
    ]
    assert main_turns[0].started_at == 100
    # The subagent's own turn stays its own turn (its own turn id).
    assert [turn.turn_id for turn in turns if turn.turn_id == "t-sub"] == ["t-sub"]


async def test_materialize_two_standalone_carriers_are_two_turns() -> None:
    """Two standalone outbound carriers (SendFileToUserTool's production
    shape: ``turn_id=""``) must replay as TWO turns in record order — one
    per sent file, each with its own attachment and timestamp — not merge
    into one turn holding both files at the earliest timestamp (the
    post-cutover regression).
    """
    store = _make_store()
    await store.append("conv.main", _msg("conv.main", "hi", timestamp=100))
    await store.append("conv.main", AttachmentCarrier(
        session_id="conv.main", agent_name="main", timestamp_ms=200,
        attachments=[{"id": "att-1", "kind": "other", "name": "a.txt",
                      "mime": "text/plain", "size": 1, "path": "/x/a.txt",
                      "locator": "workspace"}]))
    await store.append("conv.main", AttachmentCarrier(
        session_id="conv.main", agent_name="main", timestamp_ms=300,
        attachments=[{"id": "att-2", "kind": "image", "name": "b.png",
                      "mime": "image/png", "size": 2, "path": "/x/b.png",
                      "locator": "workspace"}]))

    turns = await store.load_materialized_by_prefix("conv")

    assert len(turns) == 2, (
        f"expected one turn per standalone carrier, got {len(turns)}: "
        f"{[(t.turn_id, t.attachments) for t in turns]}"
    )
    assert turns[0].turn_id == ""
    assert turns[0].blocks == []
    assert [att["id"] for att in turns[0].attachments] == ["att-1"]
    assert turns[0].started_at == 200
    assert turns[1].turn_id == ""
    assert turns[1].blocks == []
    assert [att["id"] for att in turns[1].attachments] == ["att-2"]
    assert turns[1].started_at == 300


# ── ResilientTranscriptStore (I/O resilience) ───────────────────────────────


class _FlakyDelegate(TranscriptStore):
    """In-memory delegate whose ``append`` fails on demand."""

    def __init__(self) -> None:
        self.records: list[tuple[str, TranscriptRecord]] = []
        self.fail_next: bool = False

    async def append(
        self, session_id: str, event: TranscriptRecord, *, pool: str | None = None
    ) -> None:
        del pool
        if self.fail_next:
            self.fail_next = False
            raise OSError("simulated disk full")
        self.records.append((session_id, event))

    async def load(self, session_id: str) -> list[TranscriptRecord]:
        return [evt for sid, evt in self.records if sid == session_id]

    async def load_sessions_by_prefix(
        self, session_prefix: str, *, pool: str | None = None
    ) -> list[TranscriptRecord]:
        del pool
        return [
            evt
            for sid, evt in self.records
            if sid.split(".", 1)[0] == session_prefix
        ]

    async def list_sessions(self) -> set[str]:
        return {sid for sid, _ in self.records}

    async def list_sessions_by_prefix(self, session_prefix: str) -> set[str]:
        return {
            sid
            for sid in await self.list_sessions()
            if sid.split(".", 1)[0] == session_prefix
        }

    async def delete_session(self, session_id: str) -> None:
        self.records = [(s, e) for s, e in self.records if s != session_id]

    async def delete_sessions_by_prefix(self, session_prefix: str) -> None:
        self.records = [(s, e) for s, e in self.records if s.split(".", 1)[0] != session_prefix]


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

    assert [e for _, e in delegate.records]  # the recovery write landed
    assert delegate.records[0][1].content == "kept"


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

    store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")

    async def _boom(
        self: object, session_id: str, event: object, *, pool: str | None = None
    ) -> None:
        del pool
        raise OSError("disk full")

    monkeypatch.setattr(JSONLTranscriptStore, "append", _boom)

    # Must NOT raise — proving the resilient wrapper sits in the write path.
    with bind_workspace_root(tmp_path):
        await store.append("conv.main", _msg("conv.main", "hi"))
