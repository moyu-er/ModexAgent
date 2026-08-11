"""InboxMQ conformance — same assertions for ``file`` and ``sqlite`` backends.

File: :class:`LocalFileInboxMQ`.
SQLite: :class:`SqliteInboxMQ` (over ``ConnectionManager``).
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path

import pytest

from modex_agent.core.scope import RecordScope
from modex_agent.multi_agent.inbox.server import InboxMQ
from modex_agent.multi_agent.inbox.server_local import LocalFileInboxMQ
from modex_agent.multi_agent.inbox.types import InboxMessage
from modex_agent.persistence import ConnectionManager, DatabaseKind
from modex_agent.persistence.adapters.inbox_mq import SqliteInboxMQ

_MSG_TYPE = "agent_message"


def _msg(
    mid: str = "m1",
    session: str = "s1",
    content: str = "hello",
    mtype: str = _MSG_TYPE,
) -> InboxMessage:
    return InboxMessage(
        session_id=session,
        source="agent_a",
        content=content,
        message_type=mtype,
        message_id=mid,
    )


@pytest.fixture(params=["file", "sqlite"])
async def mq(
    request: pytest.FixtureRequest,
    tmp_path: Path,
) -> AsyncGenerator[InboxMQ]:
    """Parametrized InboxMQ — file (LocalFileInboxMQ) or sqlite (SqliteInboxMQ)."""
    if request.param == "file":
        yield LocalFileInboxMQ(workspace=tmp_path / "inbox_file")
        return
    db_path = tmp_path / "workspace.db"
    mgr = ConnectionManager(db_path, DatabaseKind.WORKSPACE)
    await mgr.open()
    yield SqliteInboxMQ(
        db_path=db_path,
        scope=RecordScope(workspace_id="conformance"),
        connection=mgr,
    )
    await mgr.close()


class TestInboxMQConformance:
    """Same behavior on both backends."""

    async def test_receive_new_returns_true(self, mq: InboxMQ) -> None:
        assert await mq.receive("s1", _msg()) is True

    async def test_receive_duplicate_returns_false(self, mq: InboxMQ) -> None:
        await mq.receive("s1", _msg(mid="m1"))
        assert await mq.receive("s1", _msg(mid="m1")) is False

    async def test_count_empty_is_zero(self, mq: InboxMQ) -> None:
        assert await mq.count("s1") == 0

    async def test_count_after_receive(self, mq: InboxMQ) -> None:
        await mq.receive("s1", _msg(mid="m1"))
        await mq.receive("s1", _msg(mid="m2"))
        assert await mq.count("s1") == 2

    async def test_peek_non_destructive(self, mq: InboxMQ) -> None:
        await mq.receive("s1", _msg(mid="m1"))
        peeked = await mq.peek("s1")
        assert len(peeked) == 1
        assert peeked[0].message_id == "m1"
        # peek does not consume
        assert await mq.count("s1") == 1

    async def test_contains_pending_true_before_consume(self, mq: InboxMQ) -> None:
        await mq.receive("s1", _msg(mid="m1"))

        assert await mq.contains_pending("s1", "m1") is True

    async def test_contains_pending_false_after_consume(self, mq: InboxMQ) -> None:
        await mq.receive("s1", _msg(mid="m1"))
        await mq.consume("s1")

        assert await mq.contains_pending("s1", "m1") is False

    async def test_consume_removes_messages(self, mq: InboxMQ) -> None:
        await mq.receive("s1", _msg(mid="m1"))
        await mq.receive("s1", _msg(mid="m2"))
        consumed = await mq.consume("s1")
        assert len(consumed) == 2
        assert {m.message_id for m in consumed} == {"m1", "m2"}
        assert await mq.count("s1") == 0

    async def test_consume_fifo_order(self, mq: InboxMQ) -> None:
        await mq.receive("s1", _msg(mid="m1", content="first"))
        await mq.receive("s1", _msg(mid="m2", content="second"))
        consumed = await mq.consume("s1")
        assert [m.content for m in consumed] == ["first", "second"]

    async def test_consume_then_receive_same_id_rejected(self, mq: InboxMQ) -> None:
        await mq.receive("s1", _msg(mid="m1"))
        await mq.consume("s1")
        # after consume, the delivered-id prevents re-delivery
        assert await mq.receive("s1", _msg(mid="m1")) is False

    async def test_consume_only_types_filters(self, mq: InboxMQ) -> None:
        await mq.receive("s1", _msg(mid="m1", mtype="agent_message"))
        await mq.receive("s1", _msg(mid="m2", mtype="external_input"))
        consumed = await mq.consume("s1", only_types={"agent_message"})
        assert len(consumed) == 1
        assert consumed[0].message_id == "m1"
        # non-matching stays pending
        assert await mq.count("s1") == 1

    async def test_clear_removes_pending(self, mq: InboxMQ) -> None:
        await mq.receive("s1", _msg(mid="m1"))
        await mq.receive("s1", _msg(mid="m2"))
        await mq.clear("s1")
        assert await mq.count("s1") == 0

    async def test_sessions_with_pending(self, mq: InboxMQ) -> None:
        await mq.receive("s1", _msg(mid="m1"))
        await mq.receive("s2", _msg(mid="m2"))
        pending = await mq.sessions_with_pending()
        assert set(pending) == {"s1", "s2"}

    async def test_sessions_with_pending_excludes_empty(self, mq: InboxMQ) -> None:
        await mq.receive("s1", _msg(mid="m1"))
        await mq.consume("s1")
        pending = await mq.sessions_with_pending()
        assert pending == []

    async def test_deliver_new_returns_true(self, mq: InboxMQ) -> None:
        assert mq.deliver("s1", _msg(mid="d1")) is True

    async def test_deliver_duplicate_returns_false(self, mq: InboxMQ) -> None:
        mq.deliver("s1", _msg(mid="d1"))
        assert mq.deliver("s1", _msg(mid="d1")) is False

    async def test_deliver_then_consume(self, mq: InboxMQ) -> None:
        mq.deliver("s1", _msg(mid="d1", content="via_deliver"))
        consumed = await mq.consume("s1")
        assert len(consumed) == 1
        assert consumed[0].content == "via_deliver"

    async def test_deliver_after_consume_rejected(self, mq: InboxMQ) -> None:
        await mq.receive("s1", _msg(mid="m1"))
        await mq.consume("s1")
        assert mq.deliver("s1", _msg(mid="m1")) is False

    async def test_reap_expired_returns_int(self, mq: InboxMQ) -> None:
        result = await mq.reap_expired()
        assert isinstance(result, int)
