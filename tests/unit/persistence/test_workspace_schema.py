from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from modex_agent.core.scope import RecordScope
from modex_agent.persistence import ConnectionManager, DatabaseKind

EXPECTED_TABLES: frozenset[str] = frozenset(
    {
        "sessions",
        "pool_routing",
        "inbox_topics",
        "inbox_messages",
        "inbox_delivered_ids",
        "inbox_dead_letter",
        "turn_snapshots",
        "approval_audit_log",
        "todos",
        "memory_session_messages",
        "memory_kv",
        "memory_cursors",
        "memory_revisions",
        "memory_archive_state",
        "memory_archive_entries",
        "external_session_map",
        "workspace_meta",
    }
)


def _scope(**fields: str | None) -> str:
    """Build a canonical-ish JSON scope string for test inserts."""
    return json.dumps({k: v for k, v in fields.items() if v is not None})


@pytest.mark.asyncio
async def test_all_workspace_tables_exist(tmp_path: Path) -> None:
    manager = ConnectionManager(tmp_path / "state.db", DatabaseKind.WORKSPACE)
    await manager.open()

    rows = await manager.query_all(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    )
    table_names = {row[0] for row in rows}
    await manager.close()

    missing = EXPECTED_TABLES - table_names
    assert not missing, f"missing workspace tables: {sorted(missing)}"


@pytest.mark.asyncio
async def test_generated_scope_columns_derived_from_json(tmp_path: Path) -> None:
    manager = ConnectionManager(tmp_path / "state.db", DatabaseKind.WORKSPACE)
    await manager.open()

    scope = _scope(
        pool="default",
        agent_id="main",
        session_prefix="sess.abc",
        session_id="sess.abc",
    )
    await manager.execute(
        "INSERT INTO sessions (session_id, scope) VALUES (?, ?)",
        ("sess.abc", scope),
    )
    row = await manager.query_one(
        "SELECT pool, agent_id, session_prefix, invocation_id, parent_session_id "
        "FROM sessions WHERE session_id = ?",
        ("sess.abc",),
    )
    await manager.close()

    assert row is not None
    assert row[0] == "default"
    assert row[1] == "main"
    assert row[2] == "sess.abc"
    assert row[3] is None
    assert row[4] is None


@pytest.mark.asyncio
async def test_partial_unique_index_rejects_duplicate_active_turn(tmp_path: Path) -> None:
    manager = ConnectionManager(tmp_path / "state.db", DatabaseKind.WORKSPACE)
    await manager.open()

    scope = _scope(pool="default", session_prefix="s1")
    insert_sql = (
        "INSERT INTO turn_snapshots "
        "(session_id, agent_id, turn_id, scope, phase, created_at, updated_at, payload_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
    )

    await manager.execute(insert_sql, ("s1", "main", "t1", scope, "running", 1.0, 1.0, "{}"))

    with pytest.raises(sqlite3.IntegrityError):
        await manager.execute(insert_sql, ("s1", "main", "t2", scope, "running", 2.0, 2.0, "{}"))

    # A completed turn for the same (agent_id, session_id) is allowed.
    await manager.execute(insert_sql, ("s1", "main", "t3", scope, "completed", 3.0, 3.0, "{}"))

    count = await manager.query_value(
        "SELECT COUNT(*) FROM turn_snapshots WHERE session_id = ? AND agent_id = ?",
        int,
        ("s1", "main"),
    )
    await manager.close()

    assert count == 2


@pytest.mark.asyncio
async def test_partial_unique_index_rejects_duplicate_suspended_turn(tmp_path: Path) -> None:
    """The partial unique index also covers 'suspended' phase."""
    manager = ConnectionManager(tmp_path / "state.db", DatabaseKind.WORKSPACE)
    await manager.open()

    scope = _scope(pool="default", session_prefix="s2")
    insert_sql = (
        "INSERT INTO turn_snapshots "
        "(session_id, agent_id, turn_id, scope, phase, created_at, updated_at, payload_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
    )

    await manager.execute(insert_sql, ("s2", "agent_a", "t1", scope, "running", 1.0, 1.0, "{}"))

    with pytest.raises(sqlite3.IntegrityError):
        await manager.execute(
            insert_sql, ("s2", "agent_a", "t2", scope, "suspended", 2.0, 2.0, "{}")
        )

    await manager.close()


@pytest.mark.asyncio
async def test_check_constraint_rejects_invalid_memory_state(tmp_path: Path) -> None:
    manager = ConnectionManager(tmp_path / "state.db", DatabaseKind.WORKSPACE)
    await manager.open()

    scope = _scope(pool="default")
    with pytest.raises(sqlite3.IntegrityError):
        await manager.execute(
            "INSERT INTO memory_session_messages "
            "(scope_key, scope, seq, role, message_json, created_at, state) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("k1", scope, 1, "user", "{}", 1.0, "invalid_state"),
        )

    await manager.close()


@pytest.mark.asyncio
async def test_check_constraint_enforces_deleted_at_state_consistency(tmp_path: Path) -> None:
    """(state = 'soft_deleted') must equal (deleted_at IS NOT NULL)."""
    manager = ConnectionManager(tmp_path / "state.db", DatabaseKind.WORKSPACE)
    await manager.open()

    scope = _scope(pool="default")

    # 'normal' with non-null deleted_at must be rejected.
    with pytest.raises(sqlite3.IntegrityError):
        await manager.execute(
            "INSERT INTO memory_session_messages "
            "(scope_key, scope, seq, role, message_json, created_at, state, deleted_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("k1", scope, 1, "user", "{}", 1.0, "normal", 1.0),
        )

    # 'soft_deleted' with null deleted_at must be rejected.
    with pytest.raises(sqlite3.IntegrityError):
        await manager.execute(
            "INSERT INTO memory_session_messages "
            "(scope_key, scope, seq, role, message_json, created_at, state, deleted_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("k2", scope, 1, "user", "{}", 1.0, "soft_deleted", None),
        )

    # Valid: 'normal' with NULL deleted_at.
    await manager.execute(
        "INSERT INTO memory_session_messages "
        "(scope_key, scope, seq, role, message_json, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("k3", scope, 1, "user", "{}", 1.0),
    )

    # Valid: 'soft_deleted' with non-null deleted_at.
    await manager.execute(
        "INSERT INTO memory_session_messages "
        "(scope_key, scope, seq, role, message_json, created_at, state, deleted_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("k4", scope, 1, "user", "{}", 1.0, "soft_deleted", 2.0),
    )

    count = await manager.query_value("SELECT COUNT(*) FROM memory_session_messages", int)
    await manager.close()

    assert count == 2


@pytest.mark.asyncio
async def test_json_valid_rejects_non_json_scope(tmp_path: Path) -> None:
    """Non-JSON scope is rejected — either by the CHECK constraint (IntegrityError)
    or by the json_extract generated column (OperationalError), depending on which
    SQLite evaluates first. Both prevent bad data from being stored."""
    manager = ConnectionManager(tmp_path / "state.db", DatabaseKind.WORKSPACE)
    await manager.open()

    with pytest.raises((sqlite3.IntegrityError, sqlite3.OperationalError)):
        await manager.execute(
            "INSERT INTO pool_routing (session_prefix, pool_name, scope) VALUES (?, ?, ?)",
            ("s1", "default", "not-json"),
        )

    await manager.close()


@pytest.mark.asyncio
async def test_inbox_topics_accept_poolless_canonical_scope(tmp_path: Path) -> None:
    manager = ConnectionManager(tmp_path / "state.db", DatabaseKind.WORKSPACE)
    await manager.open()

    owner_scope_key = RecordScope(workspace_id="workspace-a").canonical()
    scope_key = RecordScope(workspace_id="workspace-a", session_id="sess.1").canonical()
    await manager.execute(
        "INSERT INTO inbox_topics "
        "(owner_scope_key, scope_key, session_id, scope, created_at, last_active) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (owner_scope_key, scope_key, "sess.1", scope_key, 1.0, 1.0),
    )

    row = await manager.query_one("SELECT pool FROM inbox_topics")
    await manager.close()

    assert row is not None
    assert row[0] is None


@pytest.mark.asyncio
async def test_inbox_topics_require_exact_scope_key(tmp_path: Path) -> None:
    manager = ConnectionManager(tmp_path / "state.db", DatabaseKind.WORKSPACE)
    await manager.open()

    owner_scope_key = RecordScope(workspace_id="workspace-a").canonical()
    scope_key = RecordScope(workspace_id="workspace-a", session_id="sess.1").canonical()
    different_scope = RecordScope(workspace_id="workspace-b", session_id="sess.1").canonical()

    with pytest.raises(sqlite3.IntegrityError):
        await manager.execute(
            "INSERT INTO inbox_topics "
            "(owner_scope_key, scope_key, session_id, scope, created_at, last_active) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (owner_scope_key, scope_key, "sess.1", different_scope, 1.0, 1.0),
        )

    await manager.close()


@pytest.mark.asyncio
async def test_inbox_topics_require_canonical_owner_and_session_scope(tmp_path: Path) -> None:
    manager = ConnectionManager(tmp_path / "state.db", DatabaseKind.WORKSPACE)
    await manager.open()

    owner_scope_key = RecordScope(workspace_id="workspace-a").canonical()
    scope_without_session = RecordScope(workspace_id="workspace-a").canonical()
    insert = (
        "INSERT INTO inbox_topics "
        "(owner_scope_key, scope_key, session_id, scope, created_at, last_active) "
        "VALUES (?, ?, ?, ?, ?, ?)"
    )

    with pytest.raises(sqlite3.IntegrityError):
        await manager.execute(
            insert,
            (owner_scope_key, scope_without_session, "sess.1", scope_without_session, 1.0, 1.0),
        )

    with pytest.raises((sqlite3.IntegrityError, sqlite3.OperationalError)):
        await manager.execute(
            insert,
            ("not-json", scope_without_session, "sess.1", scope_without_session, 1.0, 1.0),
        )

    await manager.close()


@pytest.mark.asyncio
async def test_inbox_topic_and_message_ids_are_isolated_by_scope_key(tmp_path: Path) -> None:
    manager = ConnectionManager(tmp_path / "state.db", DatabaseKind.WORKSPACE)
    await manager.open()

    owner_scope_key = RecordScope(workspace_id="workspace-a").canonical()
    first_scope_key = RecordScope(
        workspace_id="workspace-a", session_id="shared", agent_id="agent-a"
    ).canonical()
    second_scope_key = RecordScope(
        workspace_id="workspace-a", session_id="shared", agent_id="agent-b"
    ).canonical()
    topic_insert = (
        "INSERT INTO inbox_topics "
        "(owner_scope_key, scope_key, session_id, scope, created_at, last_active) "
        "VALUES (?, ?, ?, ?, ?, ?)"
    )
    await manager.execute(
        topic_insert,
        (owner_scope_key, first_scope_key, "shared", first_scope_key, 1.0, 1.0),
    )
    await manager.execute(
        topic_insert,
        (owner_scope_key, second_scope_key, "shared", second_scope_key, 1.0, 1.0),
    )
    topic_rows = await manager.query_all(
        "SELECT topic_id, scope_key FROM inbox_topics ORDER BY topic_id"
    )
    message_insert = (
        "INSERT INTO inbox_messages "
        "(topic_id, owner_scope_key, scope_key, session_id, scope, message_id, message_type, "
        "source_name, content, seq, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )
    for topic_row in topic_rows:
        await manager.execute(
            message_insert,
            (
                topic_row[0],
                owner_scope_key,
                topic_row[1],
                "shared",
                topic_row[1],
                "message-1",
                "agent_message",
                "sender",
                "content",
                1,
                1.0,
            ),
        )

    count = await manager.query_value("SELECT COUNT(*) FROM inbox_messages", int)
    await manager.close()

    assert count == 2


@pytest.mark.asyncio
async def test_inbox_children_enforce_owner_and_scope_foreign_keys(tmp_path: Path) -> None:
    manager = ConnectionManager(tmp_path / "state.db", DatabaseKind.WORKSPACE)
    await manager.open()

    owner_scope_key = RecordScope(workspace_id="workspace-a").canonical()
    other_owner_scope_key = RecordScope(workspace_id="workspace-b").canonical()
    scope_key = RecordScope(workspace_id="workspace-a", session_id="sess.1").canonical()
    await manager.execute(
        "INSERT INTO inbox_topics "
        "(owner_scope_key, scope_key, session_id, scope, created_at, last_active) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (owner_scope_key, scope_key, "sess.1", scope_key, 1.0, 1.0),
    )
    topic_id = await manager.query_value("SELECT topic_id FROM inbox_topics", int)

    with pytest.raises(sqlite3.IntegrityError):
        await manager.execute(
            "INSERT INTO inbox_messages "
            "(topic_id, owner_scope_key, scope_key, session_id, scope, message_id, "
            "message_type, source_name, content, seq, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                topic_id,
                other_owner_scope_key,
                scope_key,
                "sess.1",
                scope_key,
                "message-1",
                "agent_message",
                "sender",
                "content",
                1,
                1.0,
            ),
        )

    with pytest.raises(sqlite3.IntegrityError):
        await manager.execute(
            "INSERT INTO inbox_delivered_ids "
            "(owner_scope_key, scope_key, session_id, message_id, delivered_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (other_owner_scope_key, scope_key, "sess.1", "message-1", 1.0),
        )

    await manager.close()


@pytest.mark.asyncio
async def test_inbox_delivered_id_session_matches_scoped_topic(tmp_path: Path) -> None:
    manager = ConnectionManager(tmp_path / "state.db", DatabaseKind.WORKSPACE)
    await manager.open()

    owner_scope_key = RecordScope(workspace_id="workspace-a").canonical()
    scope_key = RecordScope(workspace_id="workspace-a", session_id="sess.1").canonical()
    await manager.execute(
        "INSERT INTO inbox_topics "
        "(owner_scope_key, scope_key, session_id, scope, created_at, last_active) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (owner_scope_key, scope_key, "sess.1", scope_key, 1.0, 1.0),
    )

    with pytest.raises(sqlite3.IntegrityError):
        await manager.execute(
            "INSERT INTO inbox_delivered_ids "
            "(owner_scope_key, scope_key, session_id, message_id, delivered_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (owner_scope_key, scope_key, "different", "message-1", 1.0),
        )

    await manager.close()


@pytest.mark.asyncio
async def test_inbox_delivered_and_dead_letter_ids_are_scope_exact(tmp_path: Path) -> None:
    manager = ConnectionManager(tmp_path / "state.db", DatabaseKind.WORKSPACE)
    await manager.open()

    owner_scope_key = RecordScope(workspace_id="workspace-a").canonical()
    first_scope_key = RecordScope(
        workspace_id="workspace-a", session_id="shared", agent_id="agent-a"
    ).canonical()
    second_scope_key = RecordScope(
        workspace_id="workspace-a", session_id="shared", agent_id="agent-b"
    ).canonical()
    topic_insert = (
        "INSERT INTO inbox_topics "
        "(owner_scope_key, scope_key, session_id, scope, created_at, last_active) "
        "VALUES (?, ?, ?, ?, ?, ?)"
    )
    delivered_insert = (
        "INSERT INTO inbox_delivered_ids "
        "(owner_scope_key, scope_key, session_id, message_id, delivered_at) "
        "VALUES (?, ?, ?, ?, ?)"
    )
    dead_letter_insert = (
        "INSERT INTO inbox_dead_letter "
        "(owner_scope_key, scope_key, session_id, scope, message_id, message_type, source_name, "
        "content, expired_reason, expired_at, original_created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )
    for scope_key in (first_scope_key, second_scope_key):
        await manager.execute(
            topic_insert,
            (owner_scope_key, scope_key, "shared", scope_key, 1.0, 1.0),
        )
        await manager.execute(
            delivered_insert,
            (owner_scope_key, scope_key, "shared", "message-1", 1.0),
        )
        await manager.execute(
            dead_letter_insert,
            (
                owner_scope_key,
                scope_key,
                "shared",
                scope_key,
                "message-1",
                "agent_message",
                "sender",
                "content",
                "expired",
                2.0,
                1.0,
            ),
        )

    delivered_count = await manager.query_value("SELECT COUNT(*) FROM inbox_delivered_ids", int)
    dead_letter_count = await manager.query_value("SELECT COUNT(*) FROM inbox_dead_letter", int)
    await manager.close()

    assert delivered_count == 2
    assert dead_letter_count == 2


@pytest.mark.asyncio
async def test_turn_snapshots_phase_check_rejects_invalid_phase(tmp_path: Path) -> None:
    manager = ConnectionManager(tmp_path / "state.db", DatabaseKind.WORKSPACE)
    await manager.open()

    scope = _scope(pool="default", session_prefix="s3")
    with pytest.raises(sqlite3.IntegrityError):
        await manager.execute(
            "INSERT INTO turn_snapshots "
            "(session_id, agent_id, turn_id, scope, phase, created_at, updated_at, payload_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("s3", "main", "t1", scope, "awaiting_approval", 1.0, 1.0, "{}"),
        )

    await manager.close()


@pytest.mark.asyncio
async def test_external_session_map_provider_kind_check(tmp_path: Path) -> None:
    manager = ConnectionManager(tmp_path / "state.db", DatabaseKind.WORKSPACE)
    await manager.open()

    scope = _scope(pool="default")
    with pytest.raises(sqlite3.IntegrityError):
        await manager.execute(
            "INSERT INTO external_session_map "
            "(modex_session_id, provider_session_id, provider_kind, scope, last_committed_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("m1", "p1", "claude", scope, 1.0),
        )

    await manager.execute(
        "INSERT INTO external_session_map "
        "(modex_session_id, provider_session_id, provider_kind, scope, last_committed_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("m1", "p1", "opencode", scope, 1.0),
    )

    await manager.close()


@pytest.mark.asyncio
async def test_workspace_meta_rejects_non_json_value(tmp_path: Path) -> None:
    manager = ConnectionManager(tmp_path / "state.db", DatabaseKind.WORKSPACE)
    await manager.open()

    with pytest.raises(sqlite3.IntegrityError):
        await manager.execute(
            "INSERT INTO workspace_meta (key, value_json, updated_at) VALUES (?, ?, ?)",
            ("k", "not-json", 1.0),
        )

    await manager.execute(
        "INSERT INTO workspace_meta (key, value_json, updated_at) VALUES (?, ?, ?)",
        ("k", '"valid-string"', 1.0),
    )

    await manager.close()


@pytest.mark.asyncio
async def test_approval_audit_log_decision_check(tmp_path: Path) -> None:
    manager = ConnectionManager(tmp_path / "state.db", DatabaseKind.WORKSPACE)
    await manager.open()

    scope = _scope(pool="default")
    with pytest.raises(sqlite3.IntegrityError):
        await manager.execute(
            "INSERT INTO approval_audit_log "
            "(turn_uuid, session_id, scope, agent_id, turn_id, tool_name, decision, decided_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("u1", "s1", scope, "main", "t1", "write_file", "maybe", 1.0),
        )

    await manager.execute(
        "INSERT INTO approval_audit_log "
        "(turn_uuid, session_id, scope, agent_id, turn_id, tool_name, decision, decided_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("u1", "s1", scope, "main", "t1", "write_file", "approved", 1.0),
    )

    await manager.close()
