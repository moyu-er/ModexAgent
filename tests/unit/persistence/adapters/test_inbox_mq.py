"""Tests for ``SqliteInboxMQ`` — the SQLite-backed ``InboxMQ`` adapter (T20).

Covers:

- ABC conformance (all 10 abstract methods implemented).
- ``receive()`` idempotency via ``UNIQUE(session_id, message_id)``.
- ``consume()`` atomicity — state transition + delivered-id recording in one
  transaction; FIFO ordering; ``only_types`` filtering.
- ``deliver()`` sync cross-process delivery — stdlib ``sqlite3``, opens its own
  short-lived connection, never reuses the async connection.
- ``deliver()`` / ``receive()`` / ``consume()`` share the same dedup store.
- ``peek()``, ``count()``, ``clear()``, ``sessions_with_pending()``.
- ``wakeup()`` / ``wait_wakeup()`` — in-process event semantics + timeout.
- ``reap_expired()`` — TTL-based cleanup.
- ``deliver()`` works without a running event loop.
- ``deliver()`` works without a ``ConnectionManager`` (CLI mode).
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from modex_agent.core.scope import RecordScope
from modex_agent.multi_agent.inbox.server import InboxMQ
from modex_agent.multi_agent.inbox.types import InboxMessage
from modex_agent.persistence import ConnectionManager, DatabaseKind
from modex_agent.persistence.adapters.inbox_mq import SqliteInboxMQ
from modex_agent.persistence.session_cleanup import SqliteSessionDatabaseCleaner

_MSG_TYPE = "agent_message"
_OWNER_SCOPE = RecordScope(pool="test_pool")


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


def _open_workspace_db(tmp_path: Path) -> Path:
    """Create a workspace DB with the full schema and return its path."""
    db_path = tmp_path / "state.db"
    manager = ConnectionManager(db_path, DatabaseKind.WORKSPACE)
    asyncio.run(manager.open())
    asyncio.run(manager.close())
    return db_path


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
async def mq(tmp_path: Path) -> AsyncIterator[SqliteInboxMQ]:
    """Yield an open ``SqliteInboxMQ`` backed by a fresh workspace DB."""
    db_path = tmp_path / "state.db"
    connection = ConnectionManager(db_path, DatabaseKind.WORKSPACE)
    await connection.open()
    yield SqliteInboxMQ(
        db_path=db_path,
        scope=_OWNER_SCOPE,
        connection=connection,
    )
    await connection.close()


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Create a workspace DB (schema applied) and return its path."""
    return _open_workspace_db(tmp_path)


# --------------------------------------------------------------------------- #
# ABC conformance
# --------------------------------------------------------------------------- #


class TestABCConformance:
    def test_is_inboxmq(self, mq: SqliteInboxMQ) -> None:
        assert isinstance(mq, InboxMQ)

    def test_all_abstract_methods_implemented(self) -> None:
        """SqliteInboxMQ has no remaining abstract methods."""
        assert len(SqliteInboxMQ.__abstractmethods__) == 0


# --------------------------------------------------------------------------- #
# receive()
# --------------------------------------------------------------------------- #


class TestReceive:
    async def test_receive_new_message_returns_true(self, mq: SqliteInboxMQ) -> None:
        assert await mq.receive("s1", _msg()) is True

    async def test_receive_duplicate_returns_false(self, mq: SqliteInboxMQ) -> None:
        await mq.receive("s1", _msg())
        assert await mq.receive("s1", _msg()) is False

    async def test_receive_different_message_id_returns_true(self, mq: SqliteInboxMQ) -> None:
        await mq.receive("s1", _msg(mid="m1"))
        assert await mq.receive("s1", _msg(mid="m2")) is True

    async def test_receive_different_session_returns_true(self, mq: SqliteInboxMQ) -> None:
        await mq.receive("s1", _msg())
        assert await mq.receive("s2", _msg()) is True

    async def test_receive_after_consume_returns_false(self, mq: SqliteInboxMQ) -> None:
        """Consumed messages are permanently deduped via delivered_ids."""
        await mq.receive("s1", _msg())
        await mq.consume("s1")
        assert await mq.receive("s1", _msg()) is False


# --------------------------------------------------------------------------- #
# consume()
# --------------------------------------------------------------------------- #


class TestConsume:
    async def test_consume_returns_messages(self, mq: SqliteInboxMQ) -> None:
        await mq.receive("s1", _msg(mid="m1"))
        msgs = await mq.consume("s1")
        assert len(msgs) == 1
        assert msgs[0].message_id == "m1"
        assert msgs[0].content == "hello"

    async def test_consume_removes_from_pending(self, mq: SqliteInboxMQ) -> None:
        await mq.receive("s1", _msg())
        await mq.consume("s1")
        assert await mq.count("s1") == 0

    async def test_consume_empty_returns_empty(self, mq: SqliteInboxMQ) -> None:
        assert await mq.consume("s1") == []

    async def test_consume_fifo_order(self, mq: SqliteInboxMQ) -> None:
        await mq.receive("s1", _msg(mid="m1", content="first"))
        await mq.receive("s1", _msg(mid="m2", content="second"))
        await mq.receive("s1", _msg(mid="m3", content="third"))
        msgs = await mq.consume("s1", limit=10)
        assert [m.message_id for m in msgs] == ["m1", "m2", "m3"]

    async def test_consume_limit(self, mq: SqliteInboxMQ) -> None:
        for i in range(5):
            await mq.receive("s1", _msg(mid=f"m{i}"))
        msgs = await mq.consume("s1", limit=2)
        assert len(msgs) == 2
        assert [m.message_id for m in msgs] == ["m0", "m1"]
        # Remaining messages still pending
        assert await mq.count("s1") == 3

    async def test_consume_only_types_filters(self, mq: SqliteInboxMQ) -> None:
        await mq.receive("s1", _msg(mid="m1", mtype="agent_message"))
        await mq.receive("s1", _msg(mid="m2", mtype="task_request"))
        await mq.receive("s1", _msg(mid="m3", mtype="agent_message"))
        msgs = await mq.consume("s1", only_types={"agent_message"})
        assert {m.message_id for m in msgs} == {"m1", "m3"}
        # Non-matching still pending
        remaining = await mq.peek("s1")
        assert [m.message_id for m in remaining] == ["m2"]

    async def test_consume_only_types_none_consumes_all(self, mq: SqliteInboxMQ) -> None:
        await mq.receive("s1", _msg(mid="m1", mtype="agent_message"))
        await mq.receive("s1", _msg(mid="m2", mtype="task_request"))
        msgs = await mq.consume("s1", only_types=None)
        assert len(msgs) == 2

    async def test_consume_records_delivered_id(self, mq: SqliteInboxMQ) -> None:
        await mq.receive("s1", _msg(mid="m1"))
        await mq.consume("s1")
        # Re-receive should be rejected
        assert await mq.receive("s1", _msg(mid="m1")) is False

    async def test_consume_preserves_metadata(self, mq: SqliteInboxMQ) -> None:
        msg_with_meta = InboxMessage(
            session_id="s1",
            source="agent_a",
            content="hello",
            message_type=_MSG_TYPE,
            message_id="m1",
            metadata={"invocation_id": "inv123", "custom": "data"},
        )
        await mq.receive("s1", msg_with_meta)
        msgs = await mq.consume("s1")
        assert msgs[0].metadata.get("invocation_id") == "inv123"
        assert msgs[0].metadata.get("custom") == "data"


# --------------------------------------------------------------------------- #
# deliver() — sync cross-process delivery
# --------------------------------------------------------------------------- #


class TestDeliver:
    def test_deliver_new_message_returns_true(self, db_path: Path) -> None:
        mq = SqliteInboxMQ(db_path=db_path, scope=_OWNER_SCOPE)
        assert mq.deliver("s1", _msg()) is True

    def test_deliver_duplicate_returns_false(self, db_path: Path) -> None:
        mq = SqliteInboxMQ(db_path=db_path, scope=_OWNER_SCOPE)
        mq.deliver("s1", _msg())
        assert mq.deliver("s1", _msg()) is False

    def test_deliver_does_not_require_event_loop(self, db_path: Path) -> None:
        """deliver() is sync — callable without a running event loop."""
        mq = SqliteInboxMQ(db_path=db_path, scope=_OWNER_SCOPE)
        assert mq.deliver("s1", _msg()) is True

    def test_deliver_without_connection_manager(self, db_path: Path) -> None:
        """deliver() works with connection=None (CLI mode)."""
        mq = SqliteInboxMQ(db_path=db_path, scope=_OWNER_SCOPE, connection=None)
        assert mq.deliver("s1", _msg()) is True

    def test_deliver_then_consume(self, mq: SqliteInboxMQ) -> None:
        """deliver() writes messages that consume() can read."""
        mq.deliver("s1", _msg())
        msgs = asyncio.run(self._consume(mq, "s1"))
        assert len(msgs) == 1
        assert msgs[0].content == "hello"

    @staticmethod
    async def _consume(mq: SqliteInboxMQ, session: str) -> list[InboxMessage]:
        return await mq.consume(session)

    async def test_deliver_after_consume_rejected(self, mq: SqliteInboxMQ) -> None:
        mq.deliver("s1", _msg())
        await mq.consume("s1")
        assert mq.deliver("s1", _msg()) is False

    async def test_deliver_and_receive_share_dedup(self, mq: SqliteInboxMQ) -> None:
        """deliver() and receive() share the same dedup store."""
        mq.deliver("s1", _msg(mid="d1"))
        assert await mq.receive("s1", _msg(mid="d1")) is False
        assert await mq.receive("s1", _msg(mid="d2")) is True
        assert mq.deliver("s1", _msg(mid="d2")) is False

    def test_deliver_uses_separate_connection(self, db_path: Path) -> None:
        """deliver() must not reuse the async ConnectionManager's connection.

        We verify by checking that deliver() works even when no
        ConnectionManager is provided at all.
        """
        mq = SqliteInboxMQ(db_path=db_path, scope=_OWNER_SCOPE, connection=None)
        assert mq.deliver("s1", _msg()) is True
        # Verify the data is in the DB via a direct sqlite3 query
        conn = sqlite3.connect(str(db_path))
        count = conn.execute(
            "SELECT COUNT(*) FROM inbox_messages WHERE session_id=?", ("s1",)
        ).fetchone()[0]
        conn.close()
        assert count == 1


# --------------------------------------------------------------------------- #
# peek() / count() / clear()
# --------------------------------------------------------------------------- #


class TestPeekCountClear:
    async def test_peek_empty(self, mq: SqliteInboxMQ) -> None:
        assert await mq.peek("s1") == []

    async def test_peek_non_destructive(self, mq: SqliteInboxMQ) -> None:
        await mq.receive("s1", _msg(mid="m1"))
        msgs = await mq.peek("s1")
        assert len(msgs) == 1
        assert await mq.count("s1") == 1

    async def test_peek_multiple(self, mq: SqliteInboxMQ) -> None:
        await mq.receive("s1", _msg(mid="m1"))
        await mq.receive("s1", _msg(mid="m2"))
        msgs = await mq.peek("s1")
        assert {m.message_id for m in msgs} == {"m1", "m2"}

    async def test_count_empty(self, mq: SqliteInboxMQ) -> None:
        assert await mq.count("s1") == 0

    async def test_count_after_receive(self, mq: SqliteInboxMQ) -> None:
        await mq.receive("s1", _msg(mid="m1"))
        await mq.receive("s1", _msg(mid="m2"))
        assert await mq.count("s1") == 2

    async def test_count_after_consume(self, mq: SqliteInboxMQ) -> None:
        await mq.receive("s1", _msg(mid="m1"))
        await mq.receive("s1", _msg(mid="m2"))
        await mq.consume("s1", limit=1)
        assert await mq.count("s1") == 1

    async def test_clear_removes_pending(self, mq: SqliteInboxMQ) -> None:
        await mq.receive("s1", _msg(mid="m1"))
        await mq.receive("s1", _msg(mid="m2"))
        await mq.clear("s1")
        assert await mq.count("s1") == 0
        assert await mq.peek("s1") == []

    async def test_clear_resets_dedup(self, mq: SqliteInboxMQ) -> None:
        """After clear(), the same message_id can be received again."""
        await mq.receive("s1", _msg(mid="m1"))
        await mq.consume("s1")
        await mq.clear("s1")
        assert await mq.receive("s1", _msg(mid="m1")) is True

    async def test_clear_does_not_affect_other_sessions(self, mq: SqliteInboxMQ) -> None:
        await mq.receive("s1", _msg(mid="m1"))
        await mq.receive("s2", _msg(mid="m1"))
        await mq.clear("s1")
        assert await mq.count("s1") == 0
        assert await mq.count("s2") == 1


# --------------------------------------------------------------------------- #
# sessions_with_pending()
# --------------------------------------------------------------------------- #


class TestSessionsWithPending:
    async def test_empty(self, mq: SqliteInboxMQ) -> None:
        assert await mq.sessions_with_pending() == []

    async def test_returns_sessions_with_pending(self, mq: SqliteInboxMQ) -> None:
        await mq.receive("s1", _msg())
        await mq.receive("s2", _msg())
        sessions = await mq.sessions_with_pending()
        assert set(sessions) == {"s1", "s2"}

    async def test_excludes_consumed_sessions(self, mq: SqliteInboxMQ) -> None:
        await mq.receive("s1", _msg())
        await mq.receive("s2", _msg())
        await mq.consume("s1")
        sessions = await mq.sessions_with_pending()
        assert sessions == ["s2"]


# --------------------------------------------------------------------------- #
# wakeup() / wait_wakeup()
# --------------------------------------------------------------------------- #


class TestWakeup:
    async def test_wakeup_wakes_waiter(self, mq: SqliteInboxMQ) -> None:
        await mq.wakeup("s1")
        assert await mq.wait_wakeup("s1", timeout=0.1) is True

    async def test_wait_wakeup_timeout_no_signal(self, mq: SqliteInboxMQ) -> None:
        assert await mq.wait_wakeup("s2", timeout=0.05) is False

    async def test_wakeup_clears_after_wait(self, mq: SqliteInboxMQ) -> None:
        await mq.wakeup("s1")
        assert await mq.wait_wakeup("s1", timeout=0.1) is True
        assert await mq.wait_wakeup("s1", timeout=0.05) is False

    async def test_concurrent_wait_and_wakeup(self, mq: SqliteInboxMQ) -> None:
        async def waiter() -> bool:
            return await mq.wait_wakeup("s1", timeout=1.0)

        task = asyncio.create_task(waiter())
        await asyncio.sleep(0.05)
        await mq.wakeup("s1")
        assert await task is True


# --------------------------------------------------------------------------- #
# reap_expired()
# --------------------------------------------------------------------------- #


class TestReapExpired:
    async def test_no_ttl_returns_zero(self, mq: SqliteInboxMQ) -> None:
        await mq.receive("s1", _msg())
        assert await mq.reap_expired() == 0
        assert await mq.count("s1") == 1

    async def test_reap_deletes_only_expired_messages(self, tmp_path: Path) -> None:
        db_path = tmp_path / "state.db"
        connection = ConnectionManager(db_path, DatabaseKind.WORKSPACE)
        await connection.open()
        mq = SqliteInboxMQ(
            db_path=db_path,
            scope=_OWNER_SCOPE,
            connection=connection,
            message_ttl_seconds=0,
        )
        await mq.receive("s1", _msg(mid="pending"))
        await mq.receive("s1", _msg(mid="expired"))
        await connection.execute(
            "UPDATE inbox_messages SET created_at = 0 WHERE owner_scope_key = ?",
            (_OWNER_SCOPE.canonical(),),
        )
        await connection.execute(
            "UPDATE inbox_messages SET state = 'expired' WHERE message_id = 'expired'"
        )

        reaped = await mq.reap_expired()

        assert reaped == 1
        assert [message.message_id for message in await mq.peek("s1")] == ["pending"]
        await connection.close()

    async def test_reap_keeps_fresh_messages(self, tmp_path: Path) -> None:
        db_path = tmp_path / "state.db"
        connection = ConnectionManager(db_path, DatabaseKind.WORKSPACE)
        await connection.open()
        mq = SqliteInboxMQ(
            db_path=db_path,
            scope=_OWNER_SCOPE,
            connection=connection,
            message_ttl_seconds=10.0,
        )
        await mq.receive("s1", _msg(mid="m1"))
        reaped = await mq.reap_expired()
        assert reaped == 0
        assert await mq.count("s1") == 1
        await connection.close()

    async def test_reap_clears_only_owner_stale_delivered_ids(self, tmp_path: Path) -> None:
        db_path = tmp_path / "state.db"
        connection = ConnectionManager(db_path, DatabaseKind.WORKSPACE)
        await connection.open()
        other_scope = RecordScope(pool="other_pool")
        owner = SqliteInboxMQ(
            db_path=db_path,
            scope=_OWNER_SCOPE,
            connection=connection,
            message_ttl_seconds=0,
        )
        other = SqliteInboxMQ(
            db_path=db_path,
            scope=other_scope,
            connection=connection,
            message_ttl_seconds=0,
        )
        await owner.receive("s1", _msg(mid="m1"))
        await other.receive("s1", _msg(mid="m1"))
        await owner.consume("s1")
        await other.consume("s1")
        await connection.execute(
            "UPDATE inbox_delivered_ids SET delivered_at = 0"
        )
        await owner.reap_expired()

        owner_count = await connection.query_value(
            "SELECT count(*) FROM inbox_delivered_ids WHERE owner_scope_key = ?",
            int,
            (_OWNER_SCOPE.canonical(),),
        )
        other_count = await connection.query_value(
            "SELECT count(*) FROM inbox_delivered_ids WHERE owner_scope_key = ?",
            int,
            (other_scope.canonical(),),
        )
        assert owner_count == 0
        assert other_count == 1
        await connection.close()

    async def test_reap_is_isolated_by_owner_scope(self, tmp_path: Path) -> None:
        db_path = tmp_path / "state.db"
        connection = ConnectionManager(db_path, DatabaseKind.WORKSPACE)
        await connection.open()
        owner_a = SqliteInboxMQ(
            db_path,
            scope=RecordScope(workspace_id="workspace_a"),
            connection=connection,
            message_ttl_seconds=0,
        )
        owner_b = SqliteInboxMQ(
            db_path,
            scope=RecordScope(workspace_id="workspace_b"),
            connection=connection,
            message_ttl_seconds=60,
        )
        await owner_a.receive("shared.agent", _msg("a", "shared.agent"))
        await owner_b.receive("shared.agent", _msg("b", "shared.agent"))
        await connection.execute(
            "UPDATE inbox_messages SET state = 'expired' WHERE owner_scope_key = ?",
            (RecordScope(workspace_id="workspace_a").canonical(),),
        )

        await owner_a.reap_expired()

        assert await owner_a.count("shared.agent") == 0
        assert await owner_b.count("shared.agent") == 1
        await connection.close()


# --------------------------------------------------------------------------- #
# Cross-method integration
# --------------------------------------------------------------------------- #


class TestIntegration:
    @pytest.mark.parametrize(
        ("owner_a", "owner_b"),
        [
            (RecordScope(pool="pool_a"), RecordScope(pool="pool_b")),
            (RecordScope(workspace_id="workspace_a"), RecordScope(workspace_id="workspace_b")),
            (
                RecordScope(session_prefix="prefix_a"),
                RecordScope(session_prefix="prefix_b"),
            ),
            (RecordScope(agent_id="agent_a"), RecordScope(agent_id="agent_b")),
            (RecordScope(agent_role="main"), RecordScope(agent_role="subagent")),
            (RecordScope(user_id="user_a"), RecordScope(user_id="user_b")),
            (RecordScope(tenant_id="tenant_a"), RecordScope(tenant_id="tenant_b")),
            (RecordScope(channel="channel_a"), RecordScope(channel="channel_b")),
            (RecordScope(chat_id="chat_a"), RecordScope(chat_id="chat_b")),
            (RecordScope(invocation_id="invocation_a"), RecordScope(invocation_id="invocation_b")),
            (
                RecordScope(parent_session_id="parent_a"),
                RecordScope(parent_session_id="parent_b"),
            ),
        ],
    )
    async def test_same_message_identity_is_isolated_by_owner_dimension(
        self,
        tmp_path: Path,
        owner_a: RecordScope,
        owner_b: RecordScope,
    ) -> None:
        db_path = tmp_path / "state.db"
        connection = ConnectionManager(db_path, DatabaseKind.WORKSPACE)
        await connection.open()
        inbox_a = SqliteInboxMQ(db_path, scope=owner_a, connection=connection)
        inbox_b = SqliteInboxMQ(db_path, scope=owner_b, connection=connection)

        assert await inbox_a.receive("shared.agent", _msg("same", "shared.agent", "a"))
        assert await inbox_b.receive("shared.agent", _msg("same", "shared.agent", "b"))
        assert [message.content for message in await inbox_a.peek("shared.agent")] == ["a"]
        assert [message.content for message in await inbox_b.peek("shared.agent")] == ["b"]

        consumed = await inbox_a.consume("shared.agent")

        assert [message.content for message in consumed] == ["a"]
        assert await inbox_a.count("shared.agent") == 0
        assert await inbox_b.count("shared.agent") == 1
        assert await inbox_a.receive("shared.agent", _msg("same", "shared.agent")) is False
        assert await inbox_b.receive("shared.agent", _msg("same", "shared.agent")) is False
        await connection.close()

    async def test_clear_and_pending_sessions_are_isolated_by_owner_scope(
        self, tmp_path: Path
    ) -> None:
        db_path = tmp_path / "state.db"
        connection = ConnectionManager(db_path, DatabaseKind.WORKSPACE)
        await connection.open()
        owner_a = SqliteInboxMQ(
            db_path, scope=RecordScope(workspace_id="workspace_a"), connection=connection
        )
        owner_b = SqliteInboxMQ(
            db_path, scope=RecordScope(workspace_id="workspace_b"), connection=connection
        )
        await owner_a.receive("shared.agent", _msg("a", "shared.agent"))
        await owner_b.receive("shared.agent", _msg("b", "shared.agent"))

        await owner_a.clear("shared.agent")

        assert await owner_a.sessions_with_pending() == []
        assert await owner_a.list_sessions() == []
        assert await owner_b.sessions_with_pending() == ["shared.agent"]
        assert await owner_b.list_sessions() == ["shared.agent"]
        assert [message.message_id for message in await owner_b.peek("shared.agent")] == ["b"]
        await connection.close()

    async def test_sync_deliver_and_async_receive_use_identical_scope_identity(
        self, db_path: Path
    ) -> None:
        owner = RecordScope(pool="pool_a", workspace_id="workspace_a", tenant_id="tenant_a")
        manager = ConnectionManager(db_path, DatabaseKind.WORKSPACE)
        await manager.open()
        inbox = SqliteInboxMQ(db_path, scope=owner, connection=manager)

        assert inbox.deliver("shared.agent", _msg("sync", "shared.agent", "a"))
        assert await inbox.receive("shared.agent", _msg("async", "shared.agent", "b"))

        with sqlite3.connect(db_path) as connection:
            scope_keys = connection.execute(
                "SELECT DISTINCT scope_key FROM inbox_messages"
            ).fetchall()
        expected_scope_key = owner.merge(RecordScope(session_id="shared.agent")).canonical()
        assert scope_keys == [(expected_scope_key,)]
        await manager.close()

    async def test_session_scope_key_is_deletable_by_exact_session_scope(
        self, tmp_path: Path
    ) -> None:
        db_path = tmp_path / "state.db"
        connection = ConnectionManager(db_path, DatabaseKind.WORKSPACE)
        await connection.open()
        owner = RecordScope(pool="main")
        session_scope = RecordScope(pool="main", session_id="abc.main")
        inbox = SqliteInboxMQ(db_path, scope=owner, connection=connection)
        await inbox.receive("abc.main", _msg("message", "abc.main"))

        stored_scope_key = await connection.query_value(
            "SELECT scope_key FROM inbox_messages WHERE message_id = 'message'",
            str,
        )
        deleted = await SqliteSessionDatabaseCleaner(connection).delete_session_rows(
            session_scope
        )

        assert stored_scope_key == session_scope.canonical()
        assert deleted == 2
        assert await connection.query_value("SELECT count(*) FROM inbox_messages", int) == 0
        await connection.close()

    async def test_metadata_cannot_override_owner_scope(self, tmp_path: Path) -> None:
        db_path = tmp_path / "state.db"
        connection = ConnectionManager(db_path, DatabaseKind.WORKSPACE)
        await connection.open()
        owner = RecordScope(
            pool="trusted_pool",
            workspace_id="trusted_workspace",
            invocation_id="trusted_invocation",
            parent_session_id="trusted_parent",
        )
        inbox = SqliteInboxMQ(db_path, scope=owner, connection=connection)
        message = _msg("same", "shared.agent")
        message.metadata.update(
            {
                "pool": "untrusted_pool",
                "workspace_id": "untrusted_workspace",
                "invocation_id": "untrusted_invocation",
                "parent_session_id": "untrusted_parent",
            }
        )

        assert await inbox.receive("shared.agent", message)

        row = await connection.query_one(
            "SELECT owner_scope_key, scope_key, payload_json FROM inbox_messages"
        )
        assert row is not None
        assert row["owner_scope_key"] == owner.canonical()
        assert row["scope_key"] == owner.merge(
            RecordScope(session_id="shared.agent")
        ).canonical()
        assert "untrusted_workspace" in row["payload_json"]
        await connection.close()

    async def test_deliver_then_receive_then_consume(self, mq: SqliteInboxMQ) -> None:
        """Full lifecycle: deliver → receive(dup, rejected) → consume."""
        assert mq.deliver("s1", _msg(mid="m1")) is True
        assert await mq.receive("s1", _msg(mid="m1")) is False
        msgs = await mq.consume("s1")
        assert len(msgs) == 1
        assert msgs[0].message_id == "m1"
        assert await mq.count("s1") == 0

    async def test_multiple_sessions_isolated(self, mq: SqliteInboxMQ) -> None:
        await mq.receive("s1", _msg(mid="m1", content="for-s1"))
        await mq.receive("s2", _msg(mid="m1", content="for-s2"))
        msgs1 = await mq.consume("s1")
        msgs2 = await mq.consume("s2")
        assert len(msgs1) == 1
        assert len(msgs2) == 1
        assert msgs1[0].content == "for-s1"
        assert msgs2[0].content == "for-s2"

    async def test_message_round_trip_preserves_fields(self, mq: SqliteInboxMQ) -> None:
        original = InboxMessage(
            session_id="s1",
            source="agent_x",
            content="test content with spaces",
            message_type="task_request",
            message_id="custom-id-123",
            metadata={"key": "value", "nested": {"a": 1}},
        )
        await mq.receive("s1", original)
        msgs = await mq.consume("s1")
        assert len(msgs) == 1
        m = msgs[0]
        assert m.source == "agent_x"
        assert m.content == "test content with spaces"
        assert m.message_type == "task_request"
        assert m.message_id == "custom-id-123"
        assert m.metadata.get("key") == "value"
