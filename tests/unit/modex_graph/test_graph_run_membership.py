"""Explicit run membership across stores, including lossless legacy migrations."""

from __future__ import annotations

import sqlite3

import pytest

from modex_graph import (
    GraphIORecord,
    GraphPayload,
    InMemoryGraphIORecordStore,
    InMemoryNodeStateStore,
    InvocationStatus,
    NullGraphIORecordStore,
    NullNodeStateStore,
    SqliteGraphIORecordStore,
    SqliteNodeStateStore,
)


@pytest.mark.parametrize("kind", ["null", "memory", "sqlite"])
def test_membership_is_recorded_by_every_store(kind: str) -> None:
    with sqlite3.connect(":memory:") as conn:
        nodes = (
            NullNodeStateStore(1)
            if kind == "null"
            else InMemoryNodeStateStore(1)
            if kind == "memory"
            else SqliteNodeStateStore(conn, 1)
        )
        io = (
            NullGraphIORecordStore()
            if kind == "null"
            else InMemoryGraphIORecordStore()
            if kind == "memory"
            else SqliteGraphIORecordStore(conn)
        )
        invocation = nodes.begin_invocation("work", graph_run_version=7)
        assert invocation.graph_run_version == 7
        nodes.complete_invocation(invocation)
        record = GraphIORecord(
            record_id=10,
            graph_instance_id=1,
            spec_id=2,
            version=9,
            graph_run_version=7,
            user_input=GraphPayload(content="input"),
            created_at=100,
        )
        io.save(record)
        io.update_output(10, [GraphPayload(content="output")])
        if kind == "null":
            assert nodes.load_latest("work") is None and io.get(10) is None
        else:
            latest = nodes.load_latest("work")
            assert latest is not None and latest.graph_run_version == 7
            assert latest.status is InvocationStatus.COMPLETED
            assert nodes.query_versions("work")[0] == latest
            saved = io.get(10)
            assert saved is not None and saved.graph_run_version == 7
            assert saved.output == [GraphPayload(content="output")]
        unscoped = nodes.begin_invocation("legacy")
        assert unscoped.graph_run_version is None


@pytest.mark.parametrize("retired_columns", [False, True])
def test_sqlite_membership_migration_preserves_unscoped_rows_and_new_membership(
    retired_columns: bool,
) -> None:
    with sqlite3.connect(":memory:") as conn:
        extra = ", state_json TEXT, suspended INTEGER" if retired_columns else ""
        conn.execute(
            "CREATE TABLE node_states (node_state_id INTEGER PRIMARY KEY, graph_instance_id INTEGER, "
            "node_id TEXT, version INTEGER, parent_version INTEGER, invocation_id INTEGER, "
            "status TEXT, created_at INTEGER, updated_at INTEGER" + extra + ")"
        )
        conn.execute(
            "INSERT INTO node_states (node_state_id, graph_instance_id, node_id, version, "
            "parent_version, invocation_id, status, created_at, updated_at) "
            "VALUES (1, 10, 'work', 0, NULL, 100, 'completed', 11, 12), "
            "(2, 10, 'work', 1, 0, 101, 'canceled', 13, 14)"
        )
        conn.execute(
            "CREATE TABLE graph_io_records (record_id INTEGER PRIMARY KEY, graph_instance_id INTEGER, "
            "spec_id INTEGER, version INTEGER, user_input_json TEXT, output_json TEXT, created_at INTEGER)"
        )
        conn.execute(
            "INSERT INTO graph_io_records VALUES (5, 10, 20, 1, ?, ?, 15)",
            ('{"content":"legacy input"}', '[{"content":"legacy output"}]'),
        )
        conn.commit()
        nodes = SqliteNodeStateStore(conn, 10)
        io = SqliteGraphIORecordStore(conn)
        old_nodes = nodes.query_versions("work")
        assert [
            (r.version, r.parent_version, r.invocation_id, r.created_at, r.updated_at)
            for r in old_nodes
        ] == [(1, 0, 101, 13, 14), (0, None, 100, 11, 12)]
        assert all(r.graph_run_version is None for r in old_nodes)
        old_io = io.get(5)
        assert old_io is not None and old_io.graph_run_version is None
        assert old_io.user_input == GraphPayload(content="legacy input")
        assert old_io.output == [GraphPayload(content="legacy output")]
        invocation = nodes.begin_invocation("work", graph_run_version=2)
        nodes.complete_invocation(invocation)
        io.save(
            GraphIORecord(
                record_id=6,
                graph_instance_id=10,
                spec_id=20,
                version=2,
                graph_run_version=2,
                created_at=16,
            )
        )
        if retired_columns:
            conn.execute("ALTER TABLE node_states ADD COLUMN state_json TEXT")
        # A second initialization must not rewrite old rows or lose new membership.
        nodes = SqliteNodeStateStore(conn, 10)
        io = SqliteGraphIORecordStore(conn)
        assert nodes.query_versions("work")[1:] == old_nodes
        assert io.get(5) == old_io
        latest_node = nodes.load_latest("work")
        latest_io = io.get(6)
        assert latest_node is not None and latest_node.graph_run_version == 2
        assert latest_io is not None and latest_io.graph_run_version == 2
