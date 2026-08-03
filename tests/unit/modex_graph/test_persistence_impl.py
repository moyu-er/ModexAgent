from __future__ import annotations

import sqlite3

import modex_graph
from modex_graph import (
    DeliverConsumptionStatus,
    GraphInstanceStatus,
    GraphMetadata,
    InvocationStatus,
)


def _metadata(status: GraphInstanceStatus = GraphInstanceStatus.RUNNING) -> GraphMetadata:
    return GraphMetadata(
        graph_instance_id=101,
        spec_id=202,
        parent_instance_id=None,
        parent_node=None,
        status=status,
        instance_seq=3,
        iteration_count=4,
        activated_sources={"worker": ["start"]},
        pending_dispatches={"worker": {"start": [{"value": 1}]}},
    )


def _save_versions(state: modex_graph.NodeState) -> None:
    state.save_invocation(101, "worker", 1001, 0, None, InvocationStatus.COMPLETED, {"value": 1})
    state.save_invocation(101, "worker", 1002, 1, 0, InvocationStatus.RUNNING, {"value": 2})


def test_null_node_state_is_noop() -> None:
    state = modex_graph.NullNodeState()

    state.write("value", 1)
    state.restore({"value": 2})
    state.save_invocation(101, "worker", 1001, 0, None, InvocationStatus.PENDING, {})

    assert state.read("value") is None
    assert state.snapshot() == {}
    assert state.has("value") is False
    assert state.load_invocation(101, "worker", 1001) is None
    assert state.load_latest(101, "worker") is None
    assert state.load_latest_completed(101, "worker") is None
    assert state.query_versions(101, "worker") == []


def test_simple_node_state_round_trips_and_filters_versions() -> None:
    state = modex_graph.SimpleNodeState()
    _save_versions(state)

    assert state.load_invocation(101, "worker", 1001) == state.query_versions(101, "worker")[1]
    assert state.load_latest(101, "worker") == state.query_versions(101, "worker")[0]
    assert state.load_latest_completed(101, "worker") is not None
    assert [record.version for record in state.query_versions(101, "worker")] == [1, 0]
    assert [
        record.version for record in state.query_versions(101, "worker", {InvocationStatus.RUNNING})
    ] == [1]


def test_sqlite_node_state_round_trips_and_creates_indexes() -> None:
    connection = sqlite3.connect(":memory:")
    state = modex_graph.SqliteNodeState(connection)
    _save_versions(state)

    latest = state.load_latest(101, "worker")
    completed = state.load_latest_completed(101, "worker")
    index_names = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'node_states'"
        ).fetchall()
    }

    assert latest is not None
    assert latest.invocation_id == 1002
    assert latest.state_json == {"value": 2}
    assert completed is not None
    assert completed.invocation_id == 1001
    assert {
        "idx_node_states_latest",
        "idx_node_states_status",
        "idx_node_states_cross",
        "idx_node_states_global",
    } <= index_names


def test_sqlite_node_state_migrates_legacy_schema_idempotently() -> None:
    connection = sqlite3.connect(":memory:")
    connection.execute(
        "CREATE TABLE node_states ("
        "node_state_id INTEGER PRIMARY KEY, graph_instance_id INTEGER NOT NULL, "
        "node_name TEXT NOT NULL, version INTEGER NOT NULL, state_json TEXT NOT NULL, "
        "created_at INTEGER NOT NULL, UNIQUE(graph_instance_id, node_name, version))"
    )

    state = modex_graph.SqliteNodeState(connection)
    state._migrate_schema()
    columns = {row[1] for row in connection.execute("PRAGMA table_info(node_states)").fetchall()}

    assert {"invocation_id", "parent_version", "status", "suspended", "updated_at"} <= columns


def test_node_state_factories_create_expected_strategies_with_shared_connection() -> None:
    connection = sqlite3.connect(":memory:")

    assert isinstance(modex_graph.NullNodeStateFactory().create(), modex_graph.NullNodeState)
    assert isinstance(modex_graph.SimpleNodeStateFactory().create(), modex_graph.SimpleNodeState)
    sqlite_state = modex_graph.SqliteNodeStateFactory(connection).create()
    assert isinstance(sqlite_state, modex_graph.SqliteNodeState)
    assert sqlite_state._conn is connection


def test_null_and_memory_graph_metadata_stores_obey_their_capabilities() -> None:
    null_store = modex_graph.NullGraphMetadataStore()
    memory_store = modex_graph.MemoryGraphMetadataStore()
    metadata = _metadata()

    null_store.save(101, metadata)
    null_store.update_status(101, GraphInstanceStatus.COMPLETED)
    memory_store.save(101, metadata)
    memory_store.update_status(101, GraphInstanceStatus.PAUSED)

    assert null_store.load(101) is None
    assert memory_store.load(101) == metadata.model_copy(
        update={"status": GraphInstanceStatus.PAUSED}
    )


def test_sqlite_graph_metadata_store_round_trips_and_updates_status() -> None:
    connection = sqlite3.connect(":memory:")
    store = modex_graph.SqliteGraphMetadataStore(connection)
    metadata = _metadata()

    store.save(101, metadata)
    store.update_status(101, GraphInstanceStatus.CRASHED)

    assert store.load(101) == metadata.model_copy(update={"status": GraphInstanceStatus.CRASHED})


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
