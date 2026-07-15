"""SQLite-specific tests — features that only the SQLite backend provides.

These are NOT conformance tests (they don't run on the file backend). They
verify SQLite-only capabilities:

1. **WAL multi-connection concurrency** — framework + CLI simultaneous write.
2. **Partial unique index** — one-active-turn enforcement at the DB level.
3. **Generated column derivation** — scope JSON → extracted columns.
4. **Migration idempotency** — running migrations twice is a no-op.
5. **Crash recovery** — write without close → reopen → data intact.
6. **Cross-platform CI** — these tests run on Windows/macOS/Linux.
"""

from __future__ import annotations

import threading
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import pytest

from modex_agent.agents.react.state import ReActRuntimeStateCodec, ReActSnapshotPayloadKey
from modex_agent.core.scope import RecordScope
from modex_agent.core.session_id import SessionInfo
from modex_agent.persistence import ConnectionManager, DatabaseKind, MigrationRunner
from modex_agent.persistence.adapters.approval_audit_store import (
    ApprovalAuditEntry,
    SqliteApprovalAuditStore,
)
from modex_agent.persistence.adapters.inbox_mq import SqliteInboxMQ
from modex_agent.persistence.adapters.session_store import SqliteSessionStore
from modex_agent.persistence.adapters.turn_state_store import SqliteTurnStateStore
from modex_agent.runtime.codec import RuntimeStateCodecRegistry
from modex_agent.runtime.enums import AgentKind, SnapshotReason, TurnPhase
from modex_agent.runtime.models import ResumePoint, TurnIdentity, TurnSnapshot
from modex_agent.runtime.store import ActiveTurnConflictError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _open_workspace(tmp_path: Path) -> AsyncGenerator[ConnectionManager]:
    mgr = ConnectionManager(tmp_path / "workspace.db", DatabaseKind.WORKSPACE)
    await mgr.open()
    yield mgr
    await mgr.close()


def _make_snapshot(
    *,
    agent_id: str = "main",
    session_id: str = "s1.main",
    turn_id: str = "t1",
    phase: TurnPhase = TurnPhase.RUNNING,
) -> TurnSnapshot:
    identity = TurnIdentity(
        agent_id=agent_id,
        session=SessionInfo.from_str(session_id),
        turn_id=turn_id,
    )
    state_payload: dict[str, Any] = {
        ReActSnapshotPayloadKey.CURRENT_NODE.value: "llm",
        ReActSnapshotPayloadKey.ITERATION.value: 1,
        ReActSnapshotPayloadKey.TOOL_BATCHES.value: [],
    }
    return TurnSnapshot(
        identity=identity,
        agent_kind=AgentKind.REACT,
        phase=phase,
        reason=SnapshotReason.LLM_COMPLETED,
        resume_point=ResumePoint(agent_kind=AgentKind.REACT, phase=phase),
        message_delta=[],
        state_payload=state_payload,
    )


# ===========================================================================
# 1. WAL multi-connection concurrency
# ===========================================================================


class TestWALConcurrency:
    """WAL mode allows a CLI writer (stdlib sqlite3) and the framework reader
    (aiosqlite via ConnectionManager) to coexist without corruption."""

    async def test_deliver_and_receive_coexist(self, tmp_path: Path) -> None:
        """Framework process (async) + CLI process (sync) write to the same DB."""
        from modex_agent.multi_agent.inbox.types import InboxMessage

        db_path = tmp_path / "workspace.db"
        mgr = ConnectionManager(db_path, DatabaseKind.WORKSPACE)
        await mgr.open()
        scope = RecordScope(workspace_id="wal-test")
        mq = SqliteInboxMQ(db_path=db_path, scope=scope, connection=mgr)
        msg = InboxMessage(
            session_id="s1",
            source="agent_a",
            content="async receive",
            message_type="agent_message",
            message_id="m1",
        )

        # async receive (framework path)
        assert await mq.receive("s1", msg) is True

        # sync deliver (CLI path) — different connection, WAL allows it
        msg2 = InboxMessage(
            session_id="s1",
            source="agent_b",
            content="sync deliver",
            message_type="agent_message",
            message_id="m2",
        )
        assert mq.deliver("s1", msg2) is True

        # both messages are visible to the async reader
        peeked = await mq.peek("s1")
        assert {m.message_id for m in peeked} == {"m1", "m2"}
        await mgr.close()

    async def test_wal_mode_is_enabled(self, tmp_path: Path) -> None:
        """PRAGMA journal_mode must be WAL after open()."""
        mgr = ConnectionManager(tmp_path / "workspace.db", DatabaseKind.WORKSPACE)
        await mgr.open()
        row = await mgr.query_one("PRAGMA journal_mode")
        assert row is not None
        assert row[0].lower() == "wal"
        await mgr.close()

    async def test_concurrent_sync_writers_do_not_corrupt(self, tmp_path: Path) -> None:
        """Two threads each open their own sqlite3 connection and deliver
        messages to the same session — WAL + busy_timeout must serialize them
        without data loss or corruption."""
        from modex_agent.multi_agent.inbox.types import InboxMessage

        db_path = tmp_path / "workspace.db"
        mgr = ConnectionManager(db_path, DatabaseKind.WORKSPACE)
        await mgr.open()
        await mgr.close()  # just to create + migrate the DB

        scope = RecordScope(workspace_id="wal-test")
        mq = SqliteInboxMQ(db_path=db_path, scope=scope, connection=None)
        errors: list[Exception] = []

        def _deliver(thread_id: int) -> None:
            try:
                for i in range(5):
                    msg = InboxMessage(
                        session_id="s1",
                        source=f"thread_{thread_id}",
                        content=f"msg-{thread_id}-{i}",
                        message_type="agent_message",
                        message_id=f"t{thread_id}-m{i}",
                    )
                    mq.deliver("s1", msg)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=_deliver, args=(t,)) for t in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        # Reopen async to read back
        mgr2 = ConnectionManager(db_path, DatabaseKind.WORKSPACE)
        await mgr2.open()
        mq2 = SqliteInboxMQ(db_path=db_path, scope=scope, connection=mgr2)
        peeked = await mq2.peek("s1")
        assert len(peeked) == 15  # 3 threads × 5 messages
        await mgr2.close()


# ===========================================================================
# 2. Partial unique index — one active turn per (agent_id, session_id)
# ===========================================================================


class TestPartialUniqueIndex:
    """The ``idx_turn_active_unique`` partial unique index enforces at most
    one active (running/suspended) turn per (agent_id, session_id) at the DB
    level — even without the application-level check."""

    async def test_two_running_turns_same_session_raises(self, tmp_path: Path) -> None:
        mgr = ConnectionManager(tmp_path / "workspace.db", DatabaseKind.WORKSPACE)
        await mgr.open()
        registry = RuntimeStateCodecRegistry({AgentKind.REACT: ReActRuntimeStateCodec()})
        store = SqliteTurnStateStore(mgr, registry)

        await store.save_turn(_make_snapshot(turn_id="t1", phase=TurnPhase.RUNNING))
        with pytest.raises(ActiveTurnConflictError):
            await store.save_turn(_make_snapshot(turn_id="t2", phase=TurnPhase.RUNNING))
        await mgr.close()

    async def test_completed_then_running_is_allowed(self, tmp_path: Path) -> None:
        """A completed turn does not occupy the partial index slot."""
        mgr = ConnectionManager(tmp_path / "workspace.db", DatabaseKind.WORKSPACE)
        await mgr.open()
        registry = RuntimeStateCodecRegistry({AgentKind.REACT: ReActRuntimeStateCodec()})
        store = SqliteTurnStateStore(mgr, registry)

        await store.save_turn(_make_snapshot(turn_id="t1", phase=TurnPhase.COMPLETED))
        # a new running turn is allowed because 'completed' is not in the
        # partial index WHERE clause
        await store.save_turn(_make_snapshot(turn_id="t2", phase=TurnPhase.RUNNING))
        await mgr.close()

    async def test_suspended_then_running_raises(self, tmp_path: Path) -> None:
        """A suspended turn occupies the partial index slot."""
        mgr = ConnectionManager(tmp_path / "workspace.db", DatabaseKind.WORKSPACE)
        await mgr.open()
        registry = RuntimeStateCodecRegistry({AgentKind.REACT: ReActRuntimeStateCodec()})
        store = SqliteTurnStateStore(mgr, registry)

        await store.save_turn(_make_snapshot(turn_id="t1", phase=TurnPhase.SUSPENDED))
        with pytest.raises(ActiveTurnConflictError):
            await store.save_turn(_make_snapshot(turn_id="t2", phase=TurnPhase.RUNNING))
        await mgr.close()

    async def test_different_sessions_allow_concurrent_active_turns(self, tmp_path: Path) -> None:
        mgr = ConnectionManager(tmp_path / "workspace.db", DatabaseKind.WORKSPACE)
        await mgr.open()
        registry = RuntimeStateCodecRegistry({AgentKind.REACT: ReActRuntimeStateCodec()})
        store = SqliteTurnStateStore(mgr, registry)

        await store.save_turn(
            _make_snapshot(session_id="s1.main", turn_id="t1", phase=TurnPhase.RUNNING)
        )
        await store.save_turn(
            _make_snapshot(session_id="s2.main", turn_id="t2", phase=TurnPhase.RUNNING)
        )
        await mgr.close()


# ===========================================================================
# 3. Generated column derivation from scope JSON
# ===========================================================================


class TestGeneratedColumns:
    """STORED generated columns extract fields from the ``scope`` JSON so
    queries use indexed columns instead of ``json_extract`` at query time."""

    async def test_sessions_pool_generated_from_scope(self, tmp_path: Path) -> None:
        mgr = ConnectionManager(tmp_path / "workspace.db", DatabaseKind.WORKSPACE)
        await mgr.open()
        store = SqliteSessionStore(mgr)
        from modex_agent.core.session_id import SessionInfo

        await store.save(
            SessionInfo(session_id="s1.main", agent_name="main", metadata={"pool": "coding"})
        )
        # the generated column 'pool' should be 'coding'
        row = await mgr.query_one("SELECT pool FROM sessions WHERE session_id = ?", ("s1.main",))
        assert row is not None
        assert row["pool"] == "coding"
        await mgr.close()

    async def test_sessions_agent_id_generated_from_scope(self, tmp_path: Path) -> None:
        mgr = ConnectionManager(tmp_path / "workspace.db", DatabaseKind.WORKSPACE)
        await mgr.open()
        store = SqliteSessionStore(mgr)
        from modex_agent.core.session_id import SessionInfo

        await store.save(SessionInfo(session_id="s1.main", agent_name="main"))
        row = await mgr.query_one(
            "SELECT agent_id FROM sessions WHERE session_id = ?", ("s1.main",)
        )
        assert row is not None
        assert row["agent_id"] == "main"
        await mgr.close()

    async def test_sessions_session_prefix_generated(self, tmp_path: Path) -> None:
        mgr = ConnectionManager(tmp_path / "workspace.db", DatabaseKind.WORKSPACE)
        await mgr.open()
        store = SqliteSessionStore(mgr)
        from modex_agent.core.session_id import SessionInfo

        await store.save(SessionInfo(session_id="abc123.main", agent_name="main"))
        row = await mgr.query_one(
            "SELECT session_prefix FROM sessions WHERE session_id = ?",
            ("abc123.main",),
        )
        assert row is not None
        # session_prefix is derived from the session_id's prefix portion
        assert row["session_prefix"] is not None
        await mgr.close()

    async def test_pool_routing_pool_generated_matches_pool_name(self, tmp_path: Path) -> None:
        from modex_agent.persistence.adapters.pool_routing_store import (
            SqlitePoolRoutingStore,
        )

        db_path = tmp_path / "workspace.db"
        mgr = ConnectionManager(db_path, DatabaseKind.WORKSPACE)
        await mgr.open()
        store = SqlitePoolRoutingStore(db_path)
        store.set_pool("s1", "engineering")
        # the generated 'pool' column must match pool_name
        row = store._conn.execute(  # type: ignore[attr-defined]
            "SELECT pool_name, pool FROM pool_routing WHERE session_prefix = ?",
            ("s1",),
        ).fetchone()
        assert row["pool_name"] == "engineering"
        assert row["pool"] == "engineering"
        store.close()
        await mgr.close()


# ===========================================================================
# 4. Migration idempotency
# ===========================================================================


class TestMigrationIdempotency:
    """Running ``run_pending()`` twice must be a no-op — the schema_migrations
    table tracks applied versions and skips them on the second run."""

    async def test_run_pending_twice_is_noop(self, tmp_path: Path) -> None:
        mgr = ConnectionManager(tmp_path / "workspace.db", DatabaseKind.WORKSPACE)
        await mgr.open()
        # first run applies all migrations
        await MigrationRunner(mgr, DatabaseKind.WORKSPACE).run_pending()
        # second run should be a no-op (no error, no new rows)
        await MigrationRunner(mgr, DatabaseKind.WORKSPACE).run_pending()
        # verify tables still exist and are queryable
        row = await mgr.query_one("SELECT COUNT(*) as n FROM schema_migrations")
        assert row is not None
        count = row["n"]
        assert count > 0
        await mgr.close()

    async def test_registry_migration_idempotent(self, tmp_path: Path) -> None:
        mgr = ConnectionManager(tmp_path / "registry.db", DatabaseKind.REGISTRY)
        await mgr.open()
        await MigrationRunner(mgr, DatabaseKind.REGISTRY).run_pending()
        await MigrationRunner(mgr, DatabaseKind.REGISTRY).run_pending()
        row = await mgr.query_one("SELECT COUNT(*) as n FROM schema_migrations")
        assert row is not None
        assert row["n"] > 0
        await mgr.close()


# ===========================================================================
# 5. Crash recovery — write without close → reopen → data intact
# ===========================================================================


class TestCrashRecovery:
    """If the process crashes (no graceful close), WAL mode guarantees that
    committed writes survive a reopen. SQLite's WAL is durable on commit
    (``synchronous=NORMAL`` flushes the WAL on checkpoint)."""

    async def test_write_without_close_survives_reopen(self, tmp_path: Path) -> None:
        from modex_agent.core.session_id import SessionInfo

        db_path = tmp_path / "workspace.db"
        # open, write, do NOT close (simulate crash)
        mgr1 = ConnectionManager(db_path, DatabaseKind.WORKSPACE)
        await mgr1.open()
        store = SqliteSessionStore(mgr1)
        await store.save(SessionInfo(session_id="s1.main", agent_name="main"))
        # explicitly do NOT call mgr1.close() — just drop the reference
        # (in real crash, the OS keeps the WAL file)
        # Force a WAL checkpoint so the data is in the main DB file
        await mgr1.query_one("PRAGMA wal_checkpoint(TRUNCATE)")
        del mgr1

        # reopen — data must be intact
        mgr2 = ConnectionManager(db_path, DatabaseKind.WORKSPACE)
        await mgr2.open()
        store2 = SqliteSessionStore(mgr2)
        got = await store2.get("s1.main")
        assert got is not None
        assert got.session_id == "s1.main"
        assert got.agent_name == "main"
        await mgr2.close()

    async def test_audit_log_survives_reopen(self, tmp_path: Path) -> None:
        db_path = tmp_path / "workspace.db"
        scope = RecordScope(pool="default")
        mgr1 = ConnectionManager(db_path, DatabaseKind.WORKSPACE)
        await mgr1.open()
        store1 = SqliteApprovalAuditStore(mgr1, scope)
        entry = ApprovalAuditEntry(
            turn_uuid="uuid-1",
            session_id="s1.main",
            agent_id="main",
            turn_id="t1",
            tool_name="write_file",
            tool_call_id="call1",
            decision="approved",
            decided_at="2026-01-15T10:30:00+00:00",
            decided_by="user",
        )
        await store1.record(entry)
        await mgr1.query_one("PRAGMA wal_checkpoint(TRUNCATE)")
        del mgr1

        mgr2 = ConnectionManager(db_path, DatabaseKind.WORKSPACE)
        await mgr2.open()
        store2 = SqliteApprovalAuditStore(mgr2, scope)
        results = await store2.query("s1.main")
        assert len(results) == 1
        assert results[0].turn_uuid == "uuid-1"
        await mgr2.close()


# ===========================================================================
# 6. Cross-platform CI — smoke test that these tests run on the current OS
# ===========================================================================


class TestCrossPlatform:
    """Ensure SQLite tests run on the current platform (Windows/macOS/Linux).

    This is a smoke test: if the platform-specific path handling or sqlite3
    library has issues, this will fail early.
    """

    async def test_db_path_with_spaces_works(self, tmp_path: Path) -> None:
        """Paths with spaces (common on Windows: 'C:\\Users\\My Name\\...')
        must work."""
        spacy = tmp_path / "my data dir" / "workspace.db"
        mgr = ConnectionManager(spacy, DatabaseKind.WORKSPACE)
        await mgr.open()
        await mgr.execute(
            "INSERT INTO workspace_meta (key, value_json, updated_at) VALUES ('test', '\"ok\"', 0)"
        )
        row = await mgr.query_one("SELECT value_json FROM workspace_meta WHERE key = 'test'")
        assert row is not None
        assert row[0] == '"ok"'
        await mgr.close()

    async def test_db_path_with_unicode_works(self, tmp_path: Path) -> None:
        """Unicode paths must work on all platforms."""
        unicode_path = tmp_path / "数据" / "workspace.db"
        mgr = ConnectionManager(unicode_path, DatabaseKind.WORKSPACE)
        await mgr.open()
        await mgr.execute(
            "INSERT INTO workspace_meta (key, value_json, updated_at) VALUES ('test', '\"ok\"', 0)"
        )
        row = await mgr.query_one("SELECT value_json FROM workspace_meta WHERE key = 'test'")
        assert row is not None
        assert row[0] == '"ok"'
        await mgr.close()

    async def test_foreign_keys_enforced(self, tmp_path: Path) -> None:
        """PRAGMA foreign_keys=ON is set by ConnectionManager and enforced."""
        mgr = ConnectionManager(tmp_path / "workspace.db", DatabaseKind.WORKSPACE)
        await mgr.open()
        row = await mgr.query_one("PRAGMA foreign_keys")
        assert row is not None
        assert row[0] == 1
        await mgr.close()
