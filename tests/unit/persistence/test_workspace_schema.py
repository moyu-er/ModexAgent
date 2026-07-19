"""Workspace DB schema structure tests.

Asserts the target DDL defined in
``src/modex_agent/persistence/migrations/workspace/001_initial.sql`` per
ADR-0028 (RecordScope base/subclass split, pool removal), ADR-0029 (epoch-ms
timestamps + updated_at triggers), and ADR-0031 (scope/scope_key merge,
dead-table drops, inbox_topics minimization, inbox_messages simplification).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from modex_agent.core.scope import RecordScope
from modex_agent.persistence import ConnectionManager, DatabaseKind

# ---------------------------------------------------------------------------
# Expected physical schema (target state per ADR-0028/0029/0031)
# ---------------------------------------------------------------------------

EXPECTED_TABLES: frozenset[str] = frozenset(
    {
        "memory_session_messages",
        "inbox_topics",
        "inbox_messages",
        "inbox_delivered_ids",
        "turn_snapshots",
        "sessions",
        "todos",
        "approval_audit_log",
        "memory_kv",
        "memory_cursors",
        "memory_revisions",
        "memory_archive_entries",
        "memory_archive_state",
        "external_session_map",
        "pool_routing",
    }
)

DROPPED_TABLES: frozenset[str] = frozenset({"inbox_dead_letter", "workspace_meta"})

# Tables that carry scope_key (no `scope` column anymore).
SCOPED_TABLES: frozenset[str] = frozenset(
    {
        "memory_session_messages",
        "inbox_topics",
        "inbox_messages",
        "inbox_delivered_ids",
        "turn_snapshots",
        "sessions",
        "todos",
        "approval_audit_log",
        "memory_kv",
        "memory_cursors",
        "memory_revisions",
        "memory_archive_entries",
        "memory_archive_state",
        "external_session_map",
        "pool_routing",
    }
)

# Mutable tables with `updated_at` + auto-update trigger.
MUTABLE_TABLES_WITH_TRIGGER: frozenset[str] = frozenset(
    {
        "memory_session_messages",
        "inbox_topics",
        "inbox_messages",
        "turn_snapshots",
        "sessions",
        "todos",
        "memory_kv",
        "memory_cursors",
        "memory_revisions",
        "memory_archive_state",
        "external_session_map",
        "pool_routing",
    }
)

# Append-only tables (no `updated_at`, no trigger).
APPEND_ONLY_TABLES: frozenset[str] = frozenset(
    {
        "approval_audit_log",
        "memory_archive_entries",
        "inbox_delivered_ids",
    }
)

# Dead business columns removed from `inbox_messages` per ADR-0031 §5.
INBOX_MESSAGES_DEAD_COLUMNS: frozenset[str] = frozenset(
    {
        "source_name",
        "source_kind",
        "content",
        "envelope_session_id",
        "envelope_agent_session_id",
        "scope",
        "pool",
        "agent_id",
        "session_prefix",
        "parent_session_id",
        "invocation_id",
    }
)

# `inbox_topics` minimal column set per ADR-0031 §4.
INBOX_TOPICS_COLUMNS: frozenset[str] = frozenset(
    {
        "topic_id",
        "owner_scope_key",
        "scope_key",
        "session_id",
        "created_at",
        "updated_at",
    }
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _open_workspace(tmp_path: Path) -> ConnectionManager:
    manager = ConnectionManager(tmp_path / "state.db", DatabaseKind.WORKSPACE)
    await manager.open()
    return manager


async def _table_columns(manager: ConnectionManager, table: str) -> list[str]:
    """All columns of `table`, including STORED/VIRTUAL generated ones.

    ``PRAGMA table_info`` omits generated columns on some SQLite versions; the
    ``table_xinfo`` variant includes them (with a `hidden` column differentiating
    normal/hidden/stored/virtual). We need generated-column visibility to assert
    absence of legacy `pool` / `scope` generated columns.
    """
    rows = await manager.query_all(f"PRAGMA table_xinfo({table})")
    return [row[1] for row in rows]


async def _column_type(manager: ConnectionManager, table: str, column: str) -> str | None:
    rows = await manager.query_all(f"PRAGMA table_xinfo({table})")
    for row in rows:
        if row[1] == column:
            return str(row[2])
    return None


async def _trigger_exists(manager: ConnectionManager, trigger_name: str) -> bool:
    count = await manager.query_value(
        "SELECT COUNT(*) FROM sqlite_master WHERE type = 'trigger' AND name = ?",
        int,
        (trigger_name,),
    )
    return count == 1


async def _index_exists(manager: ConnectionManager, index_name: str) -> bool:
    count = await manager.query_value(
        "SELECT COUNT(*) FROM sqlite_master WHERE type = 'index' AND name = ?",
        int,
        (index_name,),
    )
    return count == 1


def _scope_key(**fields: str | None) -> str:
    """Build a canonical scope_key JSON string (no `scope` column anymore)."""
    return RecordScope(**{k: v for k, v in fields.items() if v is not None}).canonical()


# ---------------------------------------------------------------------------
# Table presence / absence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_workspace_tables_exist(tmp_path: Path) -> None:
    manager = await _open_workspace(tmp_path)

    rows = await manager.query_all(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    )
    table_names = {row[0] for row in rows}
    await manager.close()

    missing = EXPECTED_TABLES - table_names
    assert not missing, f"missing workspace tables: {sorted(missing)}"


@pytest.mark.asyncio
async def test_dropped_tables_do_not_exist(tmp_path: Path) -> None:
    manager = await _open_workspace(tmp_path)

    rows = await manager.query_all(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN (?, ?)",
        ("inbox_dead_letter", "workspace_meta"),
    )
    found = {row[0] for row in rows}
    await manager.close()

    assert not found, f"dropped tables still present: {sorted(found)}"


# ---------------------------------------------------------------------------
# scope / scope_key merge (ADR-0031 §1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_scope_column_on_any_scoped_table(tmp_path: Path) -> None:
    manager = await _open_workspace(tmp_path)
    offenders: list[tuple[str, list[str]]] = []
    for table in SCOPED_TABLES:
        cols = await _table_columns(manager, table)
        if "scope" in cols:
            offenders.append((table, cols))
    await manager.close()

    assert not offenders, f"tables still carrying a `scope` column: {offenders}"


@pytest.mark.asyncio
async def test_scope_key_column_present_on_scoped_tables(tmp_path: Path) -> None:
    manager = await _open_workspace(tmp_path)
    missing: list[str] = []
    for table in SCOPED_TABLES:
        cols = await _table_columns(manager, table)
        if "scope_key" not in cols:
            missing.append(table)
    await manager.close()

    assert not missing, f"tables missing `scope_key` column: {sorted(missing)}"


# ---------------------------------------------------------------------------
# pool generated column removal (ADR-0028 §4)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_pool_column_on_any_table(tmp_path: Path) -> None:
    manager = await _open_workspace(tmp_path)
    rows = await manager.query_all(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    )
    offenders: list[tuple[str, list[str]]] = []
    for row in rows:
        table = row[0]
        cols = await _table_columns(manager, table)
        if "pool" in cols:
            offenders.append((table, cols))
    await manager.close()

    assert not offenders, f"tables still carrying a `pool` column: {offenders}"


# ---------------------------------------------------------------------------
# inbox_topics minimization (ADR-0031 §4)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inbox_topics_has_only_minimal_columns(tmp_path: Path) -> None:
    manager = await _open_workspace(tmp_path)
    cols = set(await _table_columns(manager, "inbox_topics"))
    await manager.close()

    assert cols == INBOX_TOPICS_COLUMNS, (
        f"inbox_topics columns must be exactly {sorted(INBOX_TOPICS_COLUMNS)}, got {sorted(cols)}"
    )


# ---------------------------------------------------------------------------
# inbox_messages simplification (ADR-0031 §5)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inbox_messages_has_no_dead_columns(tmp_path: Path) -> None:
    manager = await _open_workspace(tmp_path)
    cols = set(await _table_columns(manager, "inbox_messages"))
    await manager.close()

    leftover = INBOX_MESSAGES_DEAD_COLUMNS & cols
    assert not leftover, f"inbox_messages still carries dead columns: {sorted(leftover)}"


@pytest.mark.asyncio
async def test_inbox_messages_carries_payload_json(tmp_path: Path) -> None:
    manager = await _open_workspace(tmp_path)
    cols = set(await _table_columns(manager, "inbox_messages"))
    await manager.close()

    assert "payload_json" in cols, "inbox_messages must carry payload_json (full dict)"


# ---------------------------------------------------------------------------
# Timestamp type unification (ADR-0029 §1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_timestamp_columns_are_integer(tmp_path: Path) -> None:
    manager = await _open_workspace(tmp_path)
    offenders: list[tuple[str, str, str]] = []
    timestamp_names = {
        "created_at",
        "updated_at",
        "decided_at",
        "delivered_at",
        "consumed_at",
        "deleted_at",
        "last_committed_at",
        "last_active",
    }
    for table in EXPECTED_TABLES:
        # table_xinfo includes generated columns; table_info may not.
        rows = await manager.query_all(f"PRAGMA table_xinfo({table})")
        for row in rows:
            name = row[1]
            if name not in timestamp_names:
                continue
            col_type = str(row[2]).upper()
            if col_type != "INTEGER":
                offenders.append((table, name, col_type))
    await manager.close()

    assert not offenders, f"non-INTEGER timestamp columns: {offenders}"


# ---------------------------------------------------------------------------
# updated_at triggers (ADR-0029 §3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mutable_tables_have_auto_updated_at_trigger(tmp_path: Path) -> None:
    manager = await _open_workspace(tmp_path)
    missing: list[str] = []
    for table in MUTABLE_TABLES_WITH_TRIGGER:
        trigger = f"trg_{table}_auto_updated_at"
        if not await _trigger_exists(manager, trigger):
            missing.append(trigger)
    await manager.close()

    assert not missing, f"missing auto-updated_at triggers: {sorted(missing)}"


@pytest.mark.asyncio
async def test_append_only_tables_have_no_trigger(tmp_path: Path) -> None:
    manager = await _open_workspace(tmp_path)
    found: list[str] = []
    for table in APPEND_ONLY_TABLES:
        rows = await manager.query_all(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = ?",
            (table,),
        )
        for row in rows:
            found.append(f"{table}.{row[0]}")
    await manager.close()

    assert not found, f"append-only tables must have no trigger: {found}"


@pytest.mark.asyncio
async def test_append_only_tables_have_no_updated_at_column(tmp_path: Path) -> None:
    manager = await _open_workspace(tmp_path)
    offenders: list[str] = []
    for table in APPEND_ONLY_TABLES:
        cols = await _table_columns(manager, table)
        if "updated_at" in cols:
            offenders.append(table)
    await manager.close()

    assert not offenders, f"append-only tables must have no updated_at column: {offenders}"


@pytest.mark.asyncio
async def test_updated_at_trigger_fires_when_omitted(tmp_path: Path) -> None:
    """Omitting updated_at from UPDATE → trigger sets current time.

    Uses an explicit backdated INSERT timestamp so the trigger's auto-set value
    (epoch-ms at second resolution) is guaranteed to differ.
    """
    manager = await _open_workspace(tmp_path)

    scope_key = _scope_key(session_id="s1")
    backdated = 1  # pre-epoch sentinel; trigger's strftime-based value will exceed this.
    await manager.execute(
        "INSERT INTO todos (session_id, scope_key, items_json, updated_at) VALUES (?, ?, ?, ?)",
        ("s1", scope_key, "[]", backdated),
    )

    await manager.execute(
        "UPDATE todos SET items_json = ? WHERE session_id = ?",
        ('[{"content": "x"}]', "s1"),
    )
    after = await manager.query_value(
        "SELECT updated_at FROM todos WHERE session_id = ?", int, ("s1",)
    )
    await manager.close()

    assert after > backdated, "trigger must advance updated_at when omitted from UPDATE"


@pytest.mark.asyncio
async def test_updated_at_trigger_skips_when_explicit(tmp_path: Path) -> None:
    """Explicitly SET updated_at → trigger must not override."""
    manager = await _open_workspace(tmp_path)

    scope_key = _scope_key(session_id="s2")
    await manager.execute(
        "INSERT INTO todos (session_id, scope_key, items_json) VALUES (?, ?, ?)",
        ("s2", scope_key, "[]"),
    )

    explicit_ts = 1_700_000_000_000
    await manager.execute(
        "UPDATE todos SET items_json = ?, updated_at = ? WHERE session_id = ?",
        ('[{"content": "y"}]', explicit_ts, "s2"),
    )
    after = await manager.query_value(
        "SELECT updated_at FROM todos WHERE session_id = ?", int, ("s2",)
    )
    await manager.close()

    assert after == explicit_ts, "explicit updated_at must survive the trigger"


# ---------------------------------------------------------------------------
# Index existence (ADR-0031 §7)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_indexes_match_target_design(tmp_path: Path) -> None:
    manager = await _open_workspace(tmp_path)
    expected_indexes = {
        # memory_session_messages
        "idx_memory_session_active",
        "idx_memory_session_ttl",
        "idx_memory_session_state",
        "idx_memory_session_msg_id",
        # inbox_topics
        "idx_topics_owner",
        # inbox_messages
        "idx_messages_scope_state_seq",
        "idx_messages_owner_pending",
        "idx_messages_owner_expired",
        # inbox_delivered_ids
        "idx_delivered_owner",
        # turn_snapshots
        "idx_turn_active_unique",
        "idx_turn_session",
        "idx_turn_phase",
        "idx_turn_created",
        # sessions
        "idx_sessions_prefix",
        "idx_sessions_parent",
        # approval_audit_log
        "idx_approval_session",
        "idx_approval_turn",
        # memory_archive_entries
        "idx_archive_entries_scope_channel",
        "idx_archive_entries_scope",
        # pool_routing
        "idx_routing_pool_name",
    }
    missing: list[str] = []
    for index_name in expected_indexes:
        if not await _index_exists(manager, index_name):
            missing.append(index_name)
    await manager.close()

    assert not missing, f"missing indexes: {sorted(missing)}"


@pytest.mark.asyncio
async def test_no_pool_indexes_remain(tmp_path: Path) -> None:
    manager = await _open_workspace(tmp_path)
    rows = await manager.query_all(
        "SELECT name FROM sqlite_master WHERE type = 'index' AND name LIKE 'idx_%_pool'"
    )
    found = {row[0] for row in rows}
    await manager.close()

    # idx_routing_pool_name is the legitimate pool_name business index.
    found -= {"idx_routing_pool_name"}
    assert not found, f"leftover pool-generated indexes: {sorted(found)}"


# ---------------------------------------------------------------------------
# CHECK constraints — behavior tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_memory_session_messages_role_check(tmp_path: Path) -> None:
    manager = await _open_workspace(tmp_path)
    scope_key = _scope_key(session_id="s1")
    base_insert = (
        "INSERT INTO memory_session_messages "
        "(scope_key, seq, message_id, role, content, message_json) "
        "VALUES (?, ?, ?, ?, ?, ?)"
    )

    with pytest.raises(sqlite3.IntegrityError):
        await manager.execute(base_insert, (scope_key, 1, "m1", "developer", "hi", "{}"))

    await manager.execute(base_insert, (scope_key, 1, "m1", "user", "hi", "{}"))
    await manager.close()


@pytest.mark.asyncio
async def test_memory_session_messages_state_check(tmp_path: Path) -> None:
    manager = await _open_workspace(tmp_path)
    scope_key = _scope_key(session_id="s1")

    with pytest.raises(sqlite3.IntegrityError):
        await manager.execute(
            "INSERT INTO memory_session_messages "
            "(scope_key, seq, message_id, role, content, message_json, state) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (scope_key, 1, "m1", "user", "hi", "{}", "invalid_state"),
        )

    await manager.close()


@pytest.mark.asyncio
async def test_memory_session_messages_deleted_at_consistency(tmp_path: Path) -> None:
    """(state = 'soft_deleted') must equal (deleted_at IS NOT NULL)."""
    manager = await _open_workspace(tmp_path)
    scope_key_a = _scope_key(session_id="s1")
    scope_key_b = RecordScope(session_id="s2").canonical()
    base_insert = (
        "INSERT INTO memory_session_messages "
        "(scope_key, seq, message_id, role, content, message_json, state, deleted_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
    )

    # 'normal' with non-null deleted_at must be rejected.
    with pytest.raises(sqlite3.IntegrityError):
        await manager.execute(
            base_insert, (scope_key_a, 1, "m1", "user", "hi", "{}", "normal", 1_700_000_000_000)
        )

    # 'soft_deleted' with null deleted_at must be rejected.
    with pytest.raises(sqlite3.IntegrityError):
        await manager.execute(
            base_insert, (scope_key_b, 1, "m1", "user", "hi", "{}", "soft_deleted", None)
        )

    # Valid: 'normal' with NULL deleted_at.
    await manager.execute(
        "INSERT INTO memory_session_messages "
        "(scope_key, seq, message_id, role, content, message_json) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (scope_key_a, 1, "m1", "user", "hi", "{}"),
    )

    # Valid: 'soft_deleted' with non-null deleted_at.
    await manager.execute(
        base_insert,
        (scope_key_b, 1, "m1", "user", "hi", "{}", "soft_deleted", 1_700_000_000_000),
    )

    count = await manager.query_value("SELECT COUNT(*) FROM memory_session_messages", int)
    await manager.close()

    assert count == 2


@pytest.mark.asyncio
async def test_inbox_messages_message_type_check(tmp_path: Path) -> None:
    manager = await _open_workspace(tmp_path)
    owner = RecordScope(workspace_id="ws-a").canonical()
    scope_key = RecordScope(workspace_id="ws-a", session_id="s1").canonical()
    await manager.execute(
        "INSERT INTO inbox_topics (owner_scope_key, scope_key, session_id) VALUES (?, ?, ?)",
        (owner, scope_key, "s1"),
    )
    topic_id = await manager.query_value("SELECT topic_id FROM inbox_topics", int)

    insert = (
        "INSERT INTO inbox_messages "
        "(topic_id, owner_scope_key, scope_key, session_id, message_id, message_type, "
        "payload_json, seq) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
    )

    with pytest.raises(sqlite3.IntegrityError):
        await manager.execute(
            insert, (topic_id, owner, scope_key, "s1", "m1", "invalid_type", "{}", 1)
        )

    await manager.execute(
        insert,
        (topic_id, owner, scope_key, "s1", "m1", "agent_message", '{"src": "x"}', 1),
    )
    await manager.close()


@pytest.mark.asyncio
async def test_inbox_messages_state_check(tmp_path: Path) -> None:
    manager = await _open_workspace(tmp_path)
    owner = RecordScope(workspace_id="ws-a").canonical()
    scope_key = RecordScope(workspace_id="ws-a", session_id="s1").canonical()
    await manager.execute(
        "INSERT INTO inbox_topics (owner_scope_key, scope_key, session_id) VALUES (?, ?, ?)",
        (owner, scope_key, "s1"),
    )
    topic_id = await manager.query_value("SELECT topic_id FROM inbox_topics", int)

    insert = (
        "INSERT INTO inbox_messages "
        "(topic_id, owner_scope_key, scope_key, session_id, message_id, message_type, "
        "payload_json, seq, state) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )

    with pytest.raises(sqlite3.IntegrityError):
        await manager.execute(
            insert,
            (topic_id, owner, scope_key, "s1", "m1", "agent_message", "{}", 1, "queued"),
        )

    await manager.close()


@pytest.mark.asyncio
async def test_turn_snapshots_phase_check(tmp_path: Path) -> None:
    manager = await _open_workspace(tmp_path)
    scope_key = _scope_key(session_id="s1")

    with pytest.raises(sqlite3.IntegrityError):
        await manager.execute(
            "INSERT INTO turn_snapshots "
            "(session_id, agent_id, turn_id, scope_key, phase, payload_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("s1", "main", "t1", scope_key, "awaiting_approval", "{}"),
        )

    await manager.close()


@pytest.mark.asyncio
async def test_turn_snapshots_active_unique_partial_index(tmp_path: Path) -> None:
    """Only one running/suspended turn per (agent_id, session_id)."""
    manager = await _open_workspace(tmp_path)
    scope_key = _scope_key(session_id="s1")
    insert = (
        "INSERT INTO turn_snapshots "
        "(session_id, agent_id, turn_id, scope_key, phase, payload_json) "
        "VALUES (?, ?, ?, ?, ?, ?)"
    )

    await manager.execute(insert, ("s1", "main", "t1", scope_key, "running", "{}"))

    with pytest.raises(sqlite3.IntegrityError):
        await manager.execute(insert, ("s1", "main", "t2", scope_key, "running", "{}"))

    with pytest.raises(sqlite3.IntegrityError):
        await manager.execute(insert, ("s1", "main", "t3", scope_key, "suspended", "{}"))

    # A completed turn for the same (agent_id, session_id) is allowed.
    await manager.execute(insert, ("s1", "main", "t4", scope_key, "completed", "{}"))

    count = await manager.query_value(
        "SELECT COUNT(*) FROM turn_snapshots WHERE session_id = ? AND agent_id = ?",
        int,
        ("s1", "main"),
    )
    await manager.close()

    assert count == 2


@pytest.mark.asyncio
async def test_external_session_map_provider_kind_check(tmp_path: Path) -> None:
    manager = await _open_workspace(tmp_path)
    scope_key = _scope_key(session_id="s1")

    with pytest.raises(sqlite3.IntegrityError):
        await manager.execute(
            "INSERT INTO external_session_map "
            "(modex_session_id, scope_key, provider_session_id, provider_kind, last_committed_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("m1", scope_key, "p1", "claude", 1_700_000_000_000),
        )

    await manager.execute(
        "INSERT INTO external_session_map "
        "(modex_session_id, scope_key, provider_session_id, provider_kind, last_committed_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("m1", scope_key, "p1", "opencode", 1_700_000_000_000),
    )

    await manager.close()


@pytest.mark.asyncio
async def test_external_session_map_invalidated_check(tmp_path: Path) -> None:
    manager = await _open_workspace(tmp_path)
    scope_key = _scope_key(session_id="s1")

    with pytest.raises(sqlite3.IntegrityError):
        await manager.execute(
            "INSERT INTO external_session_map "
            "(modex_session_id, scope_key, provider_session_id, provider_kind, "
            "last_committed_at, invalidated) VALUES (?, ?, ?, ?, ?, ?)",
            ("m1", scope_key, "p1", "opencode", 1_700_000_000_000, 5),
        )

    await manager.close()


@pytest.mark.asyncio
async def test_approval_audit_log_decision_check(tmp_path: Path) -> None:
    manager = await _open_workspace(tmp_path)
    scope_key = _scope_key(session_id="s1")

    with pytest.raises(sqlite3.IntegrityError):
        await manager.execute(
            "INSERT INTO approval_audit_log "
            "(turn_uuid, session_id, scope_key, agent_id, turn_id, tool_name, decision, decided_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("u1", "s1", scope_key, "main", "t1", "write_file", "maybe", 1_700_000_000_000),
        )

    await manager.execute(
        "INSERT INTO approval_audit_log "
        "(turn_uuid, session_id, scope_key, agent_id, turn_id, tool_name, decision, decided_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("u1", "s1", scope_key, "main", "t1", "write_file", "approved", 1_700_000_000_000),
    )

    await manager.close()


# ---------------------------------------------------------------------------
# FK behavior
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inbox_messages_fk_to_inbox_topics(tmp_path: Path) -> None:
    manager = await _open_workspace(tmp_path)
    owner = RecordScope(workspace_id="ws-a").canonical()
    scope_key = RecordScope(workspace_id="ws-a", session_id="s1").canonical()
    await manager.execute(
        "INSERT INTO inbox_topics (owner_scope_key, scope_key, session_id) VALUES (?, ?, ?)",
        (owner, scope_key, "s1"),
    )
    topic_id = await manager.query_value("SELECT topic_id FROM inbox_topics", int)

    # Wrong owner_scope_key must fail FK.
    other_owner = RecordScope(workspace_id="ws-b").canonical()
    with pytest.raises(sqlite3.IntegrityError):
        await manager.execute(
            "INSERT INTO inbox_messages "
            "(topic_id, owner_scope_key, scope_key, session_id, message_id, message_type, "
            "payload_json, seq) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (topic_id, other_owner, scope_key, "s1", "m1", "agent_message", "{}", 1),
        )

    await manager.close()


@pytest.mark.asyncio
async def test_inbox_delivered_ids_fk_to_inbox_topics_scope_key(tmp_path: Path) -> None:
    """inbox_delivered_ids FK is single-column scope_key -> inbox_topics(scope_key)."""
    manager = await _open_workspace(tmp_path)
    owner = RecordScope(workspace_id="ws-a").canonical()
    scope_key = RecordScope(workspace_id="ws-a", session_id="s1").canonical()
    await manager.execute(
        "INSERT INTO inbox_topics (owner_scope_key, scope_key, session_id) VALUES (?, ?, ?)",
        (owner, scope_key, "s1"),
    )

    # FK violation: owner does not match any topic's scope_key.
    with pytest.raises(sqlite3.IntegrityError):
        await manager.execute(
            "INSERT INTO inbox_delivered_ids "
            "(scope_key, message_id, owner_scope_key, delivered_at) VALUES (?, ?, ?, ?)",
            ("not-a-known-scope-key", "m1", owner, 1_700_000_000_000),
        )

    # Valid insert.
    await manager.execute(
        "INSERT INTO inbox_delivered_ids "
        "(scope_key, message_id, owner_scope_key, delivered_at) VALUES (?, ?, ?, ?)",
        (scope_key, "m1", owner, 1_700_000_000_000),
    )

    await manager.close()


@pytest.mark.asyncio
async def test_inbox_delivered_ids_no_session_id_column(tmp_path: Path) -> None:
    """ADR-0031 §6: session_id removed from inbox_delivered_ids."""
    manager = await _open_workspace(tmp_path)
    cols = set(await _table_columns(manager, "inbox_delivered_ids"))
    await manager.close()

    assert "session_id" not in cols, "inbox_delivered_ids must not carry session_id (ADR-0031 §6)"


# ---------------------------------------------------------------------------
# Generated columns retained on `sessions` (ADR-0031 §1 — generated from scope_key)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sessions_generated_columns_derived_from_scope_key(tmp_path: Path) -> None:
    manager = await _open_workspace(tmp_path)
    scope_key = RecordScope(
        workspace_id="ws-a",
        session_prefix="sess.abc",
        agent_id="main",
        session_id="sess.abc",
        parent_session_id="parent.1",
    ).canonical()
    await manager.execute(
        "INSERT INTO sessions (session_id, scope_key) VALUES (?, ?)",
        ("sess.abc", scope_key),
    )
    row = await manager.query_one(
        "SELECT session_prefix, agent_id, parent_session_id FROM sessions WHERE session_id = ?",
        ("sess.abc",),
    )
    await manager.close()

    assert row is not None
    assert row[0] == "sess.abc"
    assert row[1] == "main"
    assert row[2] == "parent.1"


@pytest.mark.asyncio
async def test_sessions_no_pool_session_prefix_pool_index(tmp_path: Path) -> None:
    """idx_sessions_pool_prefix and idx_sessions_pool_agent must be gone."""
    manager = await _open_workspace(tmp_path)
    dead_indexes = {
        "idx_sessions_pool_prefix",
        "idx_sessions_pool_agent",
        "idx_messages_pool_session",
        "idx_messages_pool_agent",
        "idx_topics_pool",
        "idx_topics_state",
        "idx_topics_last_active",
        "idx_topics_session",
        "idx_turn_pool_session",
        "idx_turn_parent",
        "idx_routing_pool",
        "idx_todos_pool",
        "idx_memory_kv_pool",
        "idx_memory_session_pool",
        "idx_external_pool",
        "idx_approval_pool",
        "idx_dead_letter_owner_session",
        "idx_dead_letter_pool_session",
        "idx_messages_topic_state_seq",
        "idx_messages_owner_session",
        "idx_messages_scope_session",
        "idx_messages_parent",
    }
    found: list[str] = []
    for index_name in dead_indexes:
        if await _index_exists(manager, index_name):
            found.append(index_name)
    await manager.close()

    assert not found, f"dead indexes still present: {sorted(found)}"


# ---------------------------------------------------------------------------
# Defaults — `created_at` / `updated_at` auto-fill (ADR-0029 §1/§4)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_created_at_default_fires_when_omitted(tmp_path: Path) -> None:
    manager = await _open_workspace(tmp_path)
    scope_key = _scope_key(session_id="s1")
    await manager.execute(
        "INSERT INTO todos (session_id, scope_key, items_json) VALUES (?, ?, ?)",
        ("s1", scope_key, "[]"),
    )
    created_at = await manager.query_value(
        "SELECT created_at FROM todos WHERE session_id = ?", int, ("s1",)
    )
    updated_at = await manager.query_value(
        "SELECT updated_at FROM todos WHERE session_id = ?", int, ("s1",)
    )
    await manager.close()

    # Both must be positive int-ms epochs (no exact value asserted — race-free).
    assert isinstance(created_at, int) and created_at > 0
    assert isinstance(updated_at, int) and updated_at > 0
