from __future__ import annotations

import sqlite3

import modex_graph
from modex_graph import (
    DeliverConsumptionStatus,
    GraphInstanceStatus,
    GraphMetadata,
)


def _metadata(status: GraphInstanceStatus = GraphInstanceStatus.RUNNING) -> GraphMetadata:
    return GraphMetadata(
        graph_instance_id=101,
        spec_id=202,
        parent_instance_id=None,
        parent_node=None,
        status=status,
    )


def test_null_graph_instance_store_is_noop_and_load_returns_none() -> None:
    null_store = modex_graph.NullGraphInstanceStore()
    metadata = _metadata()

    null_store.save(metadata)
    null_store.update_status(101, GraphInstanceStatus.COMPLETED)
    null_store.delete(101)

    assert null_store.load(101) is None
    assert null_store.load_by_status(GraphInstanceStatus.RUNNING) == []
    assert null_store.load_by_parent(0) == []


def test_in_memory_graph_instance_store_round_trips_full_metadata_and_updates_status() -> None:
    store = modex_graph.InMemoryGraphInstanceStore()
    metadata = _metadata()

    store.save(metadata)
    assert store.load(101) == metadata

    store.update_status(101, GraphInstanceStatus.PAUSED)
    loaded = store.load(101)
    assert loaded is not None
    assert loaded.status == GraphInstanceStatus.PAUSED


def test_sqlite_graph_instance_store_round_trips_full_metadata_and_updates_status() -> None:
    conn = sqlite3.connect(":memory:")
    store = modex_graph.SqliteGraphInstanceStore(conn)
    metadata = _metadata()

    store.save(metadata)
    store.update_status(101, GraphInstanceStatus.CRASHED)

    loaded = store.load(101)
    assert loaded is not None
    assert loaded.graph_instance_id == metadata.graph_instance_id
    assert loaded.spec_id == metadata.spec_id
    assert loaded.parent_instance_id == metadata.parent_instance_id
    assert loaded.parent_node == metadata.parent_node
    assert loaded.status == GraphInstanceStatus.CRASHED
    assert loaded.created_at > 0
    assert loaded.updated_at > 0
    conn.close()


def test_deliver_factories_create_expected_strategies_with_shared_connection() -> None:
    connection = sqlite3.connect(":memory:")

    assert isinstance(modex_graph.NullDeliverStoreFactory().create(), modex_graph.NullDeliverStore)
    assert isinstance(
        modex_graph.InMemoryDeliverStoreFactory().create(), modex_graph.InMemoryDeliverStore
    )
    sqlite_store = modex_graph.SqliteDeliverStoreFactory(connection).create()
    assert isinstance(sqlite_store, modex_graph.SqliteDeliverStore)
    assert sqlite_store._conn is connection


def test_null_deliver_store_uses_queue_without_consumption_state() -> None:
    store = modex_graph.NullDeliverStore()
    first_id = store.accumulate(
        graph_instance_id=101,
        node_id="worker",
        source_node_id="start",
        source_invocation_id=1000,
        content="first",
    )
    store.accumulate(
        graph_instance_id=101,
        node_id="worker",
        source_node_id="start",
        source_invocation_id=1000,
        content="second",
    )

    store.mark_consumed([first_id], 1001)
    store.promote_consumed(1001)

    records = store.query_consumable(101, "worker")
    assert [record.content for record in records] == ["second"]
    assert records[0].status is DeliverConsumptionStatus.PENDING


def test_sqlite_deliver_store_rebuilds_old_schema_without_node_id() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE deliver_states ("
        "deliver_id INTEGER PRIMARY KEY, "
        "graph_instance_id INTEGER NOT NULL, "
        "content_json TEXT NOT NULL, "
        "status TEXT NOT NULL DEFAULT 'pending', "
        "created_at INTEGER NOT NULL, "
        "updated_at INTEGER NOT NULL)"
    )
    modex_graph.SqliteDeliverStore(conn)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(deliver_states)").fetchall()}
    assert "node_id" in columns
    assert "source_node_id" in columns
    assert "consumed_by_invocation_id" in columns
    conn.close()


def test_sqlite_graph_instance_store_rebuilds_old_schema_and_accepts_pending() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE graph_instances ("
        "graph_instance_id INTEGER PRIMARY KEY, "
        "spec_id INTEGER NOT NULL, "
        "parent_instance_id INTEGER, "
        "parent_node TEXT, "
        "status TEXT NOT NULL DEFAULT 'running', "
        "created_at INTEGER NOT NULL, "
        "updated_at INTEGER NOT NULL)"
    )
    store = modex_graph.SqliteGraphInstanceStore(conn)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(graph_instances)").fetchall()}
    assert "node_id_map_json" in columns
    store.save(_metadata(status=GraphInstanceStatus.PENDING))
    loaded = store.load(101)
    assert loaded is not None
    assert loaded.status == GraphInstanceStatus.PENDING
    conn.close()


def test_sqlite_node_state_store_rebuilds_old_schema_without_node_id() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE node_states ("
        "node_state_id INTEGER PRIMARY KEY, "
        "graph_instance_id INTEGER NOT NULL, "
        "version INTEGER NOT NULL, "
        "status TEXT NOT NULL DEFAULT 'running', "
        "state_json TEXT NOT NULL, "
        "created_at INTEGER NOT NULL, "
        "updated_at INTEGER NOT NULL)"
    )
    modex_graph.SqliteNodeStateStore(conn, 1001)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(node_states)").fetchall()}
    assert "node_id" in columns
    conn.close()
