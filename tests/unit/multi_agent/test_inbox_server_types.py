# tests/unit/multi_agent/test_inbox_server_types.py
"""InboxServer consume type-filter + sessions_with_pending."""
from __future__ import annotations
from pathlib import Path

import pytest
from modex_agent.multi_agent.inbox.server_memory import InMemoryInboxServer
from modex_agent.multi_agent.inbox.types import InboxMessage


def _msg(session_id: str, mtype: str, mid: str) -> InboxMessage:
    return InboxMessage(session_id=session_id, source="x", content="c", message_type=mtype, message_id=mid)


@pytest.mark.asyncio
async def test_consume_only_types_filters_and_keeps_others_pending() -> None:
    s = InMemoryInboxServer()
    await s.receive("s1", _msg("s1", "task_request", "1"))
    await s.receive("s1", _msg("s1", "external_input", "2"))
    await s.receive("s1", _msg("s1", "agent_message", "3"))
    got = await s.consume("s1", limit=10, only_types={"task_request", "agent_message"})
    assert [m.message_id for m in got] == ["1", "3"]
    # external_input stayed pending
    remaining = await s.peek("s1")
    assert [m.message_id for m in remaining] == ["2"]


@pytest.mark.asyncio
async def test_consume_only_types_none_consumes_all() -> None:
    s = InMemoryInboxServer()
    await s.receive("s1", _msg("s1", "task_request", "1"))
    await s.receive("s1", _msg("s1", "external_input", "2"))
    got = await s.consume("s1", limit=10)  # only_types defaults None -> all
    assert len(got) == 2


@pytest.mark.asyncio
async def test_localfile_consume_only_types_filters_and_preserves_raw_lines(
    tmp_path: Path,
) -> None:
    """Production file backend: filter + raw-line preservation for non-matches."""
    from modex_agent.multi_agent.inbox.server_local import LocalFileInboxServer

    s = LocalFileInboxServer(workspace=tmp_path, tracker=None)
    await s.receive("s1", _msg("s1", "task_request", "1"))
    await s.receive("s1", _msg("s1", "external_input", "2"))
    await s.receive("s1", _msg("s1", "agent_message", "3"))
    got = await s.consume("s1", limit=10, only_types={"task_request", "agent_message"})
    assert [m.message_id for m in got] == ["1", "3"]  # FIFO, matches only
    # external_input stayed pending via the raw-line-preservation path
    remaining = await s.peek("s1")
    assert [m.message_id for m in remaining] == ["2"]


@pytest.mark.asyncio
async def test_consume_limit_smaller_than_match_count() -> None:
    """limit caps consumed matches; other matches AND non-matches keep FIFO order."""
    s = InMemoryInboxServer()
    await s.receive("s1", _msg("s1", "task_request", "1"))
    await s.receive("s1", _msg("s1", "task_request", "2"))
    await s.receive("s1", _msg("s1", "external_input", "3"))
    await s.receive("s1", _msg("s1", "task_request", "4"))
    got = await s.consume("s1", limit=1, only_types={"task_request"})
    assert [m.message_id for m in got] == ["1"]  # only first match consumed
    # remaining two matches AND the external_input all stay pending in FIFO order
    remaining = await s.peek("s1")
    assert [m.message_id for m in remaining] == ["2", "3", "4"]


@pytest.mark.asyncio
async def test_consume_empty_only_types_consumes_nothing() -> None:
    """only_types=set() matches nothing; all messages stay pending."""
    s = InMemoryInboxServer()
    await s.receive("s1", _msg("s1", "task_request", "1"))
    await s.receive("s1", _msg("s1", "external_input", "2"))
    got = await s.consume("s1", limit=10, only_types=set())
    assert got == []
    remaining = await s.peek("s1")
    assert [m.message_id for m in remaining] == ["1", "2"]


@pytest.mark.asyncio
async def test_sessions_with_pending_excludes_empty_and_delivered_only() -> None:
    s = InMemoryInboxServer()
    await s.receive("a", _msg("a", "task_request", "1"))   # pending
    await s.receive("b", _msg("b", "task_request", "2"))
    await s.consume("b")                                    # b now delivered-only, no pending
    # c never touched
    pending = await s.sessions_with_pending()
    assert pending == ["a"]
