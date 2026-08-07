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
    assert loaded == metadata.model_copy(update={"status": GraphInstanceStatus.CRASHED})
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
        target_node="worker",
        source_node="start",
        source_invocation_id=1000,
        content="first",
    )
    store.accumulate(
        graph_instance_id=101,
        target_node="worker",
        source_node="start",
        source_invocation_id=1000,
        content="second",
    )

    store.mark_consumed([first_id], 1001)
    store.promote_consumed(1001)

    records = store.query_consumable(101, "worker")
    assert [record.content for record in records] == ["second"]
    assert records[0].status is DeliverConsumptionStatus.PENDING
