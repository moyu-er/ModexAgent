"""Approval audit vocabulary — ESCALATED decision, typed actor/source, storage.

Guard escalation must record ``ESCALATED`` — never ``APPROVED`` — and the
``source`` column (runtime actor vs delegation boundary) must round-trip
through both file (JSONL) and SQLite stores with the additive migration.
"""

from __future__ import annotations

import sqlite3
from importlib.resources import files
from pathlib import Path

from modex_agent.core.scope import RecordScope
from modex_agent.persistence import ConnectionManager, DatabaseKind
from modex_agent.persistence.adapters.approval_audit_store import (
    SqliteApprovalAuditStore,
)
from modex_agent.runtime.approval_decision import (
    ApprovalAuditDecision,
    ApprovalAuditEntry,
    ApprovalAuditSource,
    DecisionActor,
)


def _entry(**overrides: object) -> ApprovalAuditEntry:
    kwargs: dict[str, object] = {
        "turn_uuid": "u1",
        "session_id": "s1.main",
        "agent_id": "main",
        "turn_id": "t1",
        "tool_name": "write",
        "tool_call_id": "c1",
        "decision": ApprovalAuditDecision.DENIED,
        "deny_reason": "boundary escape",
        "decided_at": "2026-01-15T10:30:00+00:00",
        "decided_by": DecisionActor.SANDBOX_GUARD,
    }
    kwargs.update(overrides)
    return ApprovalAuditEntry(**kwargs)  # type: ignore[arg-type]


class TestVocabulary:
    def test_decision_has_escalated_exhaustively(self) -> None:
        assert {d.value for d in ApprovalAuditDecision} == {
            "approved",
            "denied",
            "escalated",
        }

    def test_decision_actor_members(self) -> None:
        assert DecisionActor.USER.value == "user"
        assert DecisionActor.SANDBOX_GUARD.value == "sandbox_guard"

    def test_audit_source_members(self) -> None:
        assert ApprovalAuditSource.RUNTIME.value == "runtime"
        assert ApprovalAuditSource.DELEGATION.value == "delegation"

    def test_entry_default_actor_is_user_source_runtime(self) -> None:
        entry = ApprovalAuditEntry(
            turn_uuid="u1",
            session_id="s",
            agent_id="a",
            turn_id="t",
            tool_name="w",
            tool_call_id="c",
            decision=ApprovalAuditDecision.APPROVED,
            decided_at="2026-01-15T10:30:00+00:00",
        )
        assert entry.decided_by is DecisionActor.USER
        assert entry.source is ApprovalAuditSource.RUNTIME


class TestSqliteRoundTrip:
    async def test_escalated_round_trips(self, tmp_path: Path) -> None:
        manager = ConnectionManager(tmp_path / "ws.db", DatabaseKind.WORKSPACE)
        await manager.open()
        store = SqliteApprovalAuditStore(manager, RecordScope(session_id="s1.main"))
        await store.record(
            _entry(
                decision=ApprovalAuditDecision.ESCALATED,
                deny_reason=None,
                decided_by=DecisionActor.SANDBOX_GUARD,
            )
        )
        rows = await store.query("s1.main")
        await manager.close()
        assert len(rows) == 1
        assert rows[0].decision is ApprovalAuditDecision.ESCALATED
        assert rows[0].decided_by is DecisionActor.SANDBOX_GUARD
        assert rows[0].source is ApprovalAuditSource.RUNTIME

    async def test_source_column_round_trips_delegation(self, tmp_path: Path) -> None:
        manager = ConnectionManager(tmp_path / "ws.db", DatabaseKind.WORKSPACE)
        await manager.open()
        store = SqliteApprovalAuditStore(manager, RecordScope(session_id="s1.main"))
        await store.record(_entry(source=ApprovalAuditSource.DELEGATION))
        rows = await store.query("s1.main")
        await manager.close()
        assert len(rows) == 1
        assert rows[0].source is ApprovalAuditSource.DELEGATION

    async def test_query_filters_by_source(self, tmp_path: Path) -> None:
        manager = ConnectionManager(tmp_path / "ws.db", DatabaseKind.WORKSPACE)
        await manager.open()
        store = SqliteApprovalAuditStore(manager, RecordScope(session_id="s1.main"))
        await store.record(_entry(turn_uuid="u1", source=ApprovalAuditSource.DELEGATION))
        await store.record(_entry(turn_uuid="u2"))
        rows = await store.query("s1.main", source=ApprovalAuditSource.DELEGATION)
        await manager.close()
        assert [r.turn_uuid for r in rows] == ["u1"]


class TestMigrationAddsSourceColumn:
    async def test_existing_database_preserves_rows_and_restarts_idempotently(
        self, tmp_path: Path
    ) -> None:
        db_path = tmp_path / "existing.db"
        initial = files("modex_agent.persistence.migrations").joinpath("workspace/001_initial.sql")
        with sqlite3.connect(db_path) as old:
            old.executescript(initial.read_text(encoding="utf-8"))
            old.execute(
                "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, description TEXT NOT NULL)"
            )
            old.execute("INSERT INTO schema_migrations VALUES (1, 'initial')")
            old.execute(
                "INSERT INTO approval_audit_log "
                "(id, turn_uuid, session_id, scope_key, agent_id, turn_id, tool_name, "
                "tool_call_id, decision, deny_reason, decided_at, decided_by) "
                "VALUES (41, 'u-old', 's1.main', 'original-scope', 'main', 't1', 'write', "
                "'c-old', 'denied', 'original reason', 1768473000000, 'user')"
            )
            old.execute("CREATE TABLE unrelated (value TEXT)")
            old.execute("INSERT INTO unrelated VALUES ('keep')")
            assert "source" not in {
                row[1] for row in old.execute("PRAGMA table_info(approval_audit_log)")
            }

        for restart in range(2):
            manager = ConnectionManager(db_path, DatabaseKind.WORKSPACE)
            await manager.open()
            try:
                store = SqliteApprovalAuditStore(manager, RecordScope(session_id="s1.main"))
                if restart == 0:
                    await store.record(
                        _entry(
                            turn_uuid="u-new",
                            decision=ApprovalAuditDecision.ESCALATED,
                            deny_reason=None,
                            source=ApprovalAuditSource.DELEGATION,
                        )
                    )
                rows = await store.query("s1.main")
                assert len(rows) == 2
                assert rows[0].turn_uuid == "u-old"
                assert rows[0].decision is ApprovalAuditDecision.DENIED
                assert rows[0].decided_by is DecisionActor.USER
                assert rows[0].source is ApprovalAuditSource.RUNTIME
                assert rows[0].deny_reason == "original reason"
                assert rows[1].decision is ApprovalAuditDecision.ESCALATED
                assert rows[1].source is ApprovalAuditSource.DELEGATION
                stored = await manager.query_all(
                    "SELECT id, scope_key FROM approval_audit_log ORDER BY id"
                )
                assert stored[0]["id"] == 41
                assert stored[0]["scope_key"] == "original-scope"
                assert stored[1]["id"] == 42
                assert await manager.query_value("SELECT value FROM unrelated", str) == "keep"
                assert (
                    await manager.query_value(
                        "SELECT COUNT(*) FROM schema_migrations WHERE version = 2", int
                    )
                    == 1
                )
            finally:
                await manager.close()

    async def test_source_column_exists_after_migration(self, tmp_path: Path) -> None:
        manager = ConnectionManager(tmp_path / "ws.db", DatabaseKind.WORKSPACE)
        await manager.open()
        columns = await manager.query_all("PRAGMA table_info(approval_audit_log)")
        await manager.close()
        names = {row["name"] for row in columns}
        assert "source" in names

    async def test_migration_version_two_recorded(self, tmp_path: Path) -> None:
        manager = ConnectionManager(tmp_path / "ws.db", DatabaseKind.WORKSPACE)
        await manager.open()
        version = await manager.query_value("SELECT MAX(version) FROM schema_migrations", int)
        await manager.close()
        assert version >= 2
