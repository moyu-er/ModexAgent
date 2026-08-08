"""Graph orchestration schema tests for the four tables appended to
``001_initial.sql`` (tables 16-19: graph_specs, graph_instances, node_states,
deliver_states).

Verifies the DDL for the four orchestration tables:
- Snowflake ID (BIGINT) primary keys, application-generated (not AUTOINCREMENT)
- node_states MVCC version chain (append-only, one row per version)
- deliver_states pending/consumed lifecycle
- graph_instances recursive subgraph nesting via parent_instance_id
- All CHECK constraints, UNIQUE constraints, indexes, and updated_at triggers

Uses stdlib sqlite3 only (schema tests do not need aiosqlite).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from modex_graph import SqliteDeliverStore, SqliteNodeStateStore

MIGRATION_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "modex_agent"
    / "persistence"
    / "migrations"
    / "workspace"
    / "001_initial.sql"
)

GRAPH_TABLES: frozenset[str] = frozenset(
    {
        "graph_specs",
        "graph_instances",
        "node_states",
        "deliver_states",
    }
)

VALID_STATUSES: tuple[str, ...] = (
    "running",
    "paused",
    "stopped",
    "crashed",
    "completed",
    "failed",
)

# Snowflake ID sentinels (valid 64-bit integers).
SPEC_ID = 1_700_000_000_001
INSTANCE_ID = 1_700_000_000_100
NODE_STATE_ID = 1_700_000_001_000
DELIVER_ID = 1_700_000_002_000


def _connect() -> sqlite3.Connection:
    """Open in-memory SQLite and execute the full 001_initial.sql migration."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(MIGRATION_PATH.read_text(encoding="utf-8"))
    return conn


def _seed_spec_and_instance(conn: sqlite3.Connection) -> None:
    """Insert a graph_spec + graph_instance row for child-table tests."""
    conn.execute(
        "INSERT INTO graph_specs (spec_id, name, spec_json) VALUES (?, ?, ?)",
        (SPEC_ID, "react-agent", '{"nodes": []}'),
    )
    conn.execute(
        "INSERT INTO graph_instances (graph_instance_id, spec_id) VALUES (?, ?)",
        (INSTANCE_ID, SPEC_ID),
    )
    conn.commit()


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    return row is not None


def _trigger_exists(conn: sqlite3.Connection, trigger: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'trigger' AND name = ?", (trigger,)
    ).fetchone()
    return row is not None


def _index_exists(conn: sqlite3.Connection, index: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = ?", (index,)
    ).fetchone()
    return row is not None


def _table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]


# ---------------------------------------------------------------------------
# Table presence
# ---------------------------------------------------------------------------


def test_all_four_graph_tables_exist() -> None:
    conn = _connect()
    try:
        missing = [t for t in GRAPH_TABLES if not _table_exists(conn, t)]
        assert not missing, f"missing graph orchestration tables: {missing}"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# graph_specs
# ---------------------------------------------------------------------------


def test_graph_specs_accepts_valid_insert() -> None:
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO graph_specs (spec_id, name, version, spec_json) "
            "VALUES (?, ?, ?, ?)",
            (SPEC_ID, "react-agent", "1.0", '{"nodes": []}'),
        )
        conn.commit()
        row = conn.execute(
            "SELECT spec_id, name, version FROM graph_specs WHERE spec_id = ?",
            (SPEC_ID,),
        ).fetchone()
        assert row == (SPEC_ID, "react-agent", "1.0")
    finally:
        conn.close()


def test_graph_specs_default_version_is_1_0() -> None:
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO graph_specs (spec_id, name, spec_json) VALUES (?, ?, ?)",
            (SPEC_ID, "x", "{}"),
        )
        conn.commit()
        version = conn.execute(
            "SELECT version FROM graph_specs WHERE spec_id = ?", (SPEC_ID,)
        ).fetchone()[0]
        assert version == "1.0"
    finally:
        conn.close()


def test_graph_specs_rejects_invalid_json() -> None:
    conn = _connect()
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO graph_specs (spec_id, name, spec_json) "
                "VALUES (?, ?, ?)",
                (SPEC_ID, "bad", "not-json"),
            )
    finally:
        conn.close()


def test_graph_specs_unique_name_version() -> None:
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO graph_specs (spec_id, name, version, spec_json) "
            "VALUES (?, ?, ?, ?)",
            (1, "react", "1.0", "{}"),
        )
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO graph_specs (spec_id, name, version, spec_json) "
                "VALUES (?, ?, ?, ?)",
                (2, "react", "1.0", "{}"),
            )
    finally:
        conn.close()


def test_graph_specs_same_name_different_version_allowed() -> None:
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO graph_specs (spec_id, name, version, spec_json) "
            "VALUES (?, ?, ?, ?)",
            (1, "react", "1.0", "{}"),
        )
        conn.execute(
            "INSERT INTO graph_specs (spec_id, name, version, spec_json) "
            "VALUES (?, ?, ?, ?)",
            (2, "react", "2.0", "{}"),
        )
        conn.commit()
        count = conn.execute(
            "SELECT COUNT(*) FROM graph_specs WHERE name = ?", ("react",)
        ).fetchone()[0]
        assert count == 2
    finally:
        conn.close()


def test_graph_specs_updated_at_trigger_fires_when_omitted() -> None:
    conn = _connect()
    try:
        backdated = 1
        conn.execute(
            "INSERT INTO graph_specs (spec_id, name, spec_json, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (SPEC_ID, "x", "{}", backdated),
        )
        conn.commit()
        conn.execute(
            "UPDATE graph_specs SET spec_json = ? WHERE spec_id = ?",
            ('{"v": 2}', SPEC_ID),
        )
        conn.commit()
        after = conn.execute(
            "SELECT updated_at FROM graph_specs WHERE spec_id = ?", (SPEC_ID,)
        ).fetchone()[0]
        assert after > backdated, "trigger must advance updated_at when omitted from UPDATE"
    finally:
        conn.close()


def test_graph_specs_updated_at_trigger_respects_explicit_value() -> None:
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO graph_specs (spec_id, name, spec_json) VALUES (?, ?, ?)",
            (SPEC_ID, "x", "{}"),
        )
        conn.commit()
        explicit = 1_700_000_000_000
        conn.execute(
            "UPDATE graph_specs SET spec_json = ?, updated_at = ? WHERE spec_id = ?",
            ('{"v": 2}', explicit, SPEC_ID),
        )
        conn.commit()
        after = conn.execute(
            "SELECT updated_at FROM graph_specs WHERE spec_id = ?", (SPEC_ID,)
        ).fetchone()[0]
        assert after == explicit, "explicit updated_at must survive the trigger"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# graph_instances
# ---------------------------------------------------------------------------


def test_graph_instances_accepts_valid_insert() -> None:
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO graph_specs (spec_id, name, spec_json) VALUES (?, ?, ?)",
            (SPEC_ID, "react", "{}"),
        )
        conn.execute(
            "INSERT INTO graph_instances "
            "(graph_instance_id, spec_id, parent_instance_id, parent_node, status) "
            "VALUES (?, ?, ?, ?, ?)",
            (INSTANCE_ID, SPEC_ID, None, None, "running"),
        )
        conn.commit()
        row = conn.execute(
            "SELECT status, parent_instance_id, parent_node "
            "FROM graph_instances WHERE graph_instance_id = ?",
            (INSTANCE_ID,),
        ).fetchone()
        assert row == ("running", None, None)
    finally:
        conn.close()


def test_graph_instances_default_status_is_running() -> None:
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO graph_specs (spec_id, name, spec_json) VALUES (?, ?, ?)",
            (SPEC_ID, "react", "{}"),
        )
        conn.execute(
            "INSERT INTO graph_instances (graph_instance_id, spec_id) VALUES (?, ?)",
            (INSTANCE_ID, SPEC_ID),
        )
        conn.commit()
        status = conn.execute(
            "SELECT status FROM graph_instances WHERE graph_instance_id = ?",
            (INSTANCE_ID,),
        ).fetchone()[0]
        assert status == "running"
    finally:
        conn.close()


def test_graph_instances_rejects_invalid_status() -> None:
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO graph_specs (spec_id, name, spec_json) VALUES (?, ?, ?)",
            (SPEC_ID, "react", "{}"),
        )
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO graph_instances (graph_instance_id, spec_id, status) "
                "VALUES (?, ?, ?)",
                (INSTANCE_ID, SPEC_ID, "queued"),
            )
    finally:
        conn.close()


def test_graph_instances_accepts_all_lifecycle_states() -> None:
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO graph_specs (spec_id, name, spec_json) VALUES (?, ?, ?)",
            (SPEC_ID, "react", "{}"),
        )
        for i, status in enumerate(VALID_STATUSES):
            conn.execute(
                "INSERT INTO graph_instances (graph_instance_id, spec_id, status) "
                "VALUES (?, ?, ?)",
                (INSTANCE_ID + i, SPEC_ID, status),
            )
        conn.commit()
        count = conn.execute(
            "SELECT COUNT(*) FROM graph_instances WHERE spec_id = ?", (SPEC_ID,)
        ).fetchone()[0]
        assert count == len(VALID_STATUSES)
    finally:
        conn.close()


def test_graph_instances_supports_nested_subgraph() -> None:
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO graph_specs (spec_id, name, spec_json) VALUES (?, ?, ?)",
            (SPEC_ID, "react", "{}"),
        )
        conn.execute(
            "INSERT INTO graph_instances (graph_instance_id, spec_id) VALUES (?, ?)",
            (INSTANCE_ID, SPEC_ID),
        )
        conn.execute(
            "INSERT INTO graph_instances "
            "(graph_instance_id, spec_id, parent_instance_id, parent_node) "
            "VALUES (?, ?, ?, ?)",
            (INSTANCE_ID + 1, SPEC_ID, INSTANCE_ID, "subgraph_node"),
        )
        conn.commit()
        children = conn.execute(
            "SELECT graph_instance_id, parent_node FROM graph_instances "
            "WHERE parent_instance_id = ?",
            (INSTANCE_ID,),
        ).fetchall()
        assert children == [(INSTANCE_ID + 1, "subgraph_node")]
    finally:
        conn.close()


def test_graph_instances_updated_at_trigger_fires_when_omitted() -> None:
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO graph_specs (spec_id, name, spec_json) VALUES (?, ?, ?)",
            (SPEC_ID, "react", "{}"),
        )
        conn.execute(
            "INSERT INTO graph_instances (graph_instance_id, spec_id, updated_at) "
            "VALUES (?, ?, ?)",
            (INSTANCE_ID, SPEC_ID, 1),
        )
        conn.commit()
        conn.execute(
            "UPDATE graph_instances SET status = ? WHERE graph_instance_id = ?",
            ("paused", INSTANCE_ID),
        )
        conn.commit()
        after = conn.execute(
            "SELECT updated_at FROM graph_instances WHERE graph_instance_id = ?",
            (INSTANCE_ID,),
        ).fetchone()[0]
        assert after > 1
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# node_states — MVCC version chain
# ---------------------------------------------------------------------------


def test_node_states_accepts_valid_insert() -> None:
    conn = _connect()
    try:
        _seed_spec_and_instance(conn)
        conn.execute(
            "INSERT INTO node_states "
            "(node_state_id, graph_instance_id, node_id, version, state_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (NODE_STATE_ID, INSTANCE_ID, "node-tool-call", 0, '{"step": 1}'),
        )
        conn.commit()
        row = conn.execute(
            "SELECT node_id, version FROM node_states WHERE node_state_id = ?",
            (NODE_STATE_ID,),
        ).fetchone()
        assert row == ("node-tool-call", 0)
    finally:
        conn.close()


def test_node_states_default_version_is_zero() -> None:
    conn = _connect()
    try:
        _seed_spec_and_instance(conn)
        conn.execute(
            "INSERT INTO node_states "
            "(node_state_id, graph_instance_id, node_id, state_json) "
            "VALUES (?, ?, ?, ?)",
            (NODE_STATE_ID, INSTANCE_ID, "node-tool-call", "{}"),
        )
        conn.commit()
        version = conn.execute(
            "SELECT version FROM node_states WHERE node_state_id = ?",
            (NODE_STATE_ID,),
        ).fetchone()[0]
        assert version == 0
    finally:
        conn.close()


def test_node_states_rejects_invalid_json() -> None:
    conn = _connect()
    try:
        _seed_spec_and_instance(conn)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO node_states "
                "(node_state_id, graph_instance_id, node_id, state_json) "
                "VALUES (?, ?, ?, ?)",
                (NODE_STATE_ID, INSTANCE_ID, "node-tool-call", "not-json"),
            )
    finally:
        conn.close()


def test_node_states_unique_instance_node_version() -> None:
    conn = _connect()
    try:
        _seed_spec_and_instance(conn)
        conn.execute(
            "INSERT INTO node_states "
            "(node_state_id, graph_instance_id, node_id, version, state_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (NODE_STATE_ID, INSTANCE_ID, "node-tool-call", 0, "{}"),
        )
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO node_states "
                "(node_state_id, graph_instance_id, node_id, version, state_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (NODE_STATE_ID + 1, INSTANCE_ID, "node-tool-call", 0, "{}"),
            )
    finally:
        conn.close()


def test_node_states_mvcc_keeps_all_versions() -> None:
    conn = _connect()
    try:
        _seed_spec_and_instance(conn)
        for v in range(3):
            conn.execute(
                "INSERT INTO node_states "
                "(node_state_id, graph_instance_id, node_id, version, state_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (NODE_STATE_ID + v, INSTANCE_ID, "node-tool-call", v, f'{{"v": {v}}}'),
            )
        conn.commit()
        latest = conn.execute(
            "SELECT version FROM node_states "
            "WHERE graph_instance_id = ? AND node_id = ? "
            "ORDER BY version DESC LIMIT 1",
            (INSTANCE_ID, "node-tool-call"),
        ).fetchone()[0]
        count = conn.execute(
            "SELECT COUNT(*) FROM node_states "
            "WHERE graph_instance_id = ? AND node_id = ?",
            (INSTANCE_ID, "node-tool-call"),
        ).fetchone()[0]
        assert latest == 2
        assert count == 3
    finally:
        conn.close()


def test_node_states_has_updated_at_column() -> None:
    conn = _connect()
    try:
        cols = set(_table_columns(conn, "node_states"))
        assert "updated_at" in cols, (
            "node_states must have updated_at (status lifecycle)"
        )
    finally:
        conn.close()


def test_node_states_has_no_trigger() -> None:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = ?",
            ("node_states",),
        ).fetchall()
        assert rows == [], f"node_states must have no trigger (append-only): {rows}"
    finally:
        conn.close()


def test_graph_stores_write_id_only_rows_to_workspace_schema() -> None:
    conn = _connect()
    try:
        _seed_spec_and_instance(conn)
        node_store = SqliteNodeStateStore(conn, INSTANCE_ID)
        deliver_store = SqliteDeliverStore(conn)

        invocation = node_store.begin_invocation("node-worker")
        deliver_store.accumulate(
            graph_instance_id=INSTANCE_ID,
            node_id="node-worker",
            source_node_id="node-source",
            source_invocation_id=invocation.invocation_id,
            content={"payload": "x"},
        )

        node_row = conn.execute(
            "SELECT node_name, node_id FROM node_states WHERE graph_instance_id = ?",
            (INSTANCE_ID,),
        ).fetchone()
        deliver_row = conn.execute(
            "SELECT node_name, node_id, next_node, next_node_id "
            "FROM deliver_states WHERE graph_instance_id = ?",
            (INSTANCE_ID,),
        ).fetchone()
        assert node_row == (None, "node-worker")
        assert deliver_row == (None, "node-worker", None, "node-worker")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# deliver_states
# ---------------------------------------------------------------------------


def test_deliver_states_accepts_valid_insert() -> None:
    conn = _connect()
    try:
        _seed_spec_and_instance(conn)
        conn.execute(
            "INSERT INTO deliver_states "
            "(deliver_id, graph_instance_id, node_id, next_node_id, content_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (DELIVER_ID, INSTANCE_ID, "node-accumulate", "node-downstream", '{"payload": "x"}'),
        )
        conn.commit()
        row = conn.execute(
            "SELECT status FROM deliver_states WHERE deliver_id = ?", (DELIVER_ID,)
        ).fetchone()
        assert row == ("pending",)
    finally:
        conn.close()


def test_deliver_states_default_status_is_pending() -> None:
    conn = _connect()
    try:
        _seed_spec_and_instance(conn)
        conn.execute(
            "INSERT INTO deliver_states "
            "(deliver_id, graph_instance_id, node_id, next_node_id, content_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (DELIVER_ID, INSTANCE_ID, "node-n", "node-m", "{}"),
        )
        conn.commit()
        status = conn.execute(
            "SELECT status FROM deliver_states WHERE deliver_id = ?", (DELIVER_ID,)
        ).fetchone()[0]
        assert status == "pending"
    finally:
        conn.close()


def test_deliver_states_rejects_invalid_status() -> None:
    conn = _connect()
    try:
        _seed_spec_and_instance(conn)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO deliver_states "
                "(deliver_id, graph_instance_id, node_id, next_node_id, "
                "content_json, status) VALUES (?, ?, ?, ?, ?, ?)",
                (DELIVER_ID, INSTANCE_ID, "node-n", "node-m", "{}", "invalid_status"),
            )
    finally:
        conn.close()


def test_deliver_states_rejects_invalid_json() -> None:
    conn = _connect()
    try:
        _seed_spec_and_instance(conn)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO deliver_states "
                "(deliver_id, graph_instance_id, node_id, next_node_id, content_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (DELIVER_ID, INSTANCE_ID, "node-n", "node-m", "not-json"),
            )
    finally:
        conn.close()


def test_deliver_states_lifecycle_pending_to_consumed() -> None:
    conn = _connect()
    try:
        _seed_spec_and_instance(conn)
        conn.execute(
            "INSERT INTO deliver_states "
            "(deliver_id, graph_instance_id, node_id, next_node_id, content_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (DELIVER_ID, INSTANCE_ID, "node-n", "node-m", "{}"),
        )
        conn.commit()
        conn.execute(
            "UPDATE deliver_states SET status = ? WHERE deliver_id = ?",
            ("consumed", DELIVER_ID),
        )
        conn.commit()
        status = conn.execute(
            "SELECT status FROM deliver_states WHERE deliver_id = ?", (DELIVER_ID,)
        ).fetchone()[0]
        assert status == "consumed"
    finally:
        conn.close()


def test_deliver_states_updated_at_trigger_fires_when_omitted() -> None:
    conn = _connect()
    try:
        _seed_spec_and_instance(conn)
        conn.execute(
            "INSERT INTO deliver_states "
            "(deliver_id, graph_instance_id, node_id, next_node_id, content_json, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (DELIVER_ID, INSTANCE_ID, "node-n", "node-m", "{}", 1),
        )
        conn.commit()
        conn.execute(
            "UPDATE deliver_states SET status = ? WHERE deliver_id = ?",
            ("consumed", DELIVER_ID),
        )
        conn.commit()
        after = conn.execute(
            "SELECT updated_at FROM deliver_states WHERE deliver_id = ?", (DELIVER_ID,)
        ).fetchone()[0]
        assert after > 1
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Indexes
# ---------------------------------------------------------------------------


def test_graph_indexes_exist() -> None:
    expected_indexes = {
        "idx_graph_specs_name",
        "idx_graph_instances_spec",
        "idx_graph_instances_parent",
        "idx_graph_instances_active",
        "idx_node_states_latest",
        "idx_node_states_node",
        "idx_deliver_states_node",
        "idx_deliver_states_target",
    }
    conn = _connect()
    try:
        missing = [i for i in expected_indexes if not _index_exists(conn, i)]
        assert not missing, f"missing graph indexes: {missing}"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Triggers (graph_specs, graph_instances, deliver_states — not node_states)
# ---------------------------------------------------------------------------


def test_graph_triggers_exist() -> None:
    expected_triggers = {
        "trg_graph_specs_auto_updated_at",
        "trg_graph_instances_auto_updated_at",
        "trg_deliver_states_auto_updated_at",
    }
    conn = _connect()
    try:
        missing = [t for t in expected_triggers if not _trigger_exists(conn, t)]
        assert not missing, f"missing graph triggers: {missing}"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Snowflake ID PKs — BIGINT, not AUTOINCREMENT
# ---------------------------------------------------------------------------


def test_graph_primary_keys_are_bigint_no_autoincrement() -> None:
    conn = _connect()
    try:
        for table in GRAPH_TABLES:
            rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
            pk_cols = [r for r in rows if r[5] > 0]
            assert len(pk_cols) == 1, f"{table} must have exactly one PK column"
            col_type = pk_cols[0][2].upper()
            assert col_type == "BIGINT", (
                f"{table}.{pk_cols[0][1]} must be BIGINT (Snowflake ID), got {col_type}"
            )
            sql_text = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table,),
            ).fetchone()[0]
            assert "AUTOINCREMENT" not in sql_text.upper(), (
                f"{table} PK must not use AUTOINCREMENT (Snowflake ID is app-generated)"
            )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Timestamps are INTEGER ms (ADR-0029)
# ---------------------------------------------------------------------------


def test_graph_timestamps_are_integer() -> None:
    conn = _connect()
    try:
        offenders: list[tuple[str, str, str]] = []
        for table in GRAPH_TABLES:
            for row in conn.execute(f"PRAGMA table_xinfo({table})").fetchall():
                col_name = row[1]
                col_type = str(row[2]).upper()
                if col_name in ("created_at", "updated_at") and col_type != "INTEGER":
                    offenders.append((table, col_name, col_type))
        assert not offenders, f"non-INTEGER timestamp columns: {offenders}"
    finally:
        conn.close()


def test_graph_created_at_default_fires_when_omitted() -> None:
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO graph_specs (spec_id, name, spec_json) VALUES (?, ?, ?)",
            (SPEC_ID, "x", "{}"),
        )
        conn.commit()
        created_at = conn.execute(
            "SELECT created_at FROM graph_specs WHERE spec_id = ?", (SPEC_ID,)
        ).fetchone()[0]
        assert isinstance(created_at, int) and created_at > 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# IF NOT EXISTS idempotency — re-executing the migration is a no-op
# ---------------------------------------------------------------------------

GRAPH_SCHEMA_OBJECTS: frozenset[str] = GRAPH_TABLES | {
    "idx_graph_specs_name",
    "idx_graph_instances_spec",
    "idx_graph_instances_parent",
    "idx_graph_instances_active",
    "idx_node_states_latest",
    "idx_node_states_node",
    "idx_deliver_states_node",
    "idx_deliver_states_target",
    "trg_graph_specs_auto_updated_at",
    "trg_graph_instances_auto_updated_at",
    "trg_deliver_states_auto_updated_at",
}


def test_graph_ddl_is_idempotent() -> None:
    conn = _connect()
    try:
        names = tuple(GRAPH_SCHEMA_OBJECTS)
        placeholders = ",".join("?" * len(names))
        query = (
            f"SELECT name, type, sql FROM sqlite_master "
            f"WHERE name IN ({placeholders}) ORDER BY name"
        )
        first = conn.execute(query, names).fetchall()
        conn.executescript(MIGRATION_PATH.read_text(encoding="utf-8"))
        second = conn.execute(query, names).fetchall()
        assert first == second, "re-executing migration changed graph schema objects"
        assert len(first) == len(GRAPH_SCHEMA_OBJECTS), (
            f"expected {len(GRAPH_SCHEMA_OBJECTS)} graph schema objects, got {len(first)}"
        )
    finally:
        conn.close()
