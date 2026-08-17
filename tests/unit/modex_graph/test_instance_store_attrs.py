from __future__ import annotations

import sqlite3
from collections.abc import Callable

import pytest

from modex_graph import (
    GraphInstanceStatus,
    GraphInstanceStore,
    GraphMetadata,
    InMemoryGraphInstanceStore,
    NullGraphInstanceStore,
    SqliteGraphInstanceStore,
)

_GRAPH_INSTANCE_ID = 1001
_SPEC_ID = 5001


def _metadata(
    *,
    attrs: dict[str, int | str | None] | None = None,
    version: int = 0,
) -> GraphMetadata:
    return GraphMetadata(
        graph_instance_id=_GRAPH_INSTANCE_ID,
        spec_id=_SPEC_ID,
        version=version,
        status=GraphInstanceStatus.PENDING,
        attrs={} if attrs is None else attrs,
    )


def _store_factory(kind: str) -> Callable[[], GraphInstanceStore]:
    if kind == "memory":
        return InMemoryGraphInstanceStore
    if kind == "sqlite":
        return lambda: SqliteGraphInstanceStore(sqlite3.connect(":memory:"))
    raise ValueError(f"unknown kind: {kind}")


def _store_with_prior_version_check(
    kind: str,
) -> tuple[GraphInstanceStore, Callable[[], bool]]:
    if kind == "memory":
        memory_store = InMemoryGraphInstanceStore()
        return (
            memory_store,
            lambda: memory_store._instances[_GRAPH_INSTANCE_ID][0].attrs == {"a": 1},
        )
    if kind == "sqlite":
        connection = sqlite3.connect(":memory:")
        sqlite_store = SqliteGraphInstanceStore(connection)
        return (
            sqlite_store,
            lambda: connection.execute(
                "SELECT attrs_json FROM graph_instances "
                "WHERE graph_instance_id = ? AND version = 0",
                (_GRAPH_INSTANCE_ID,),
            ).fetchone()
            == ('{"a":1}',),
        )
    raise ValueError(f"unknown kind: {kind}")


def _legacy_connection() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.execute(
        "CREATE TABLE graph_instances ("
        "graph_instance_id INTEGER NOT NULL, "
        "spec_id INTEGER NOT NULL, "
        "version INTEGER NOT NULL DEFAULT 0, "
        "parent_instance_id INTEGER, "
        "parent_node TEXT, "
        "status TEXT NOT NULL, "
        "node_id_map_json TEXT NOT NULL DEFAULT '{}', "
        "created_at INTEGER NOT NULL, "
        "updated_at INTEGER NOT NULL, "
        "PRIMARY KEY (graph_instance_id, version)"
        ")"
    )
    connection.execute(
        "INSERT INTO graph_instances VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (_GRAPH_INSTANCE_ID, _SPEC_ID, 0, None, None, "pending", "{}", 1, 1),
    )
    connection.commit()
    return connection


def test_graph_metadata_attrs_defaults_are_isolated() -> None:
    first = _metadata()
    second = _metadata()

    first.attrs["executor"] = 42

    assert second.attrs == {}


def test_null_update_attrs_is_noop() -> None:
    store = NullGraphInstanceStore()

    store.update_attrs(_GRAPH_INSTANCE_ID, {"executor": 42})

    assert store.load(_GRAPH_INSTANCE_ID) is None


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_update_attrs_round_trips_supported_values(kind: str) -> None:
    store = _store_factory(kind)()
    store.save(_metadata())

    store.update_attrs(
        _GRAPH_INSTANCE_ID,
        {"executor": 42, "host": "worker-a", "optional": None},
    )

    loaded = store.load(_GRAPH_INSTANCE_ID)
    assert loaded is not None
    assert loaded.attrs == {
        "executor": 42,
        "host": "worker-a",
        "optional": None,
    }


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_begin_invocation_copies_attrs_to_new_version(kind: str) -> None:
    store = _store_factory(kind)()
    store.save(_metadata(attrs={"executor": 42}))

    context = store.begin_invocation(_GRAPH_INSTANCE_ID)

    loaded = store.load(_GRAPH_INSTANCE_ID)
    assert context.version == 1
    assert loaded is not None
    assert loaded.version == 1
    assert loaded.attrs == {"executor": 42}


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_load_by_status_returns_attrs(kind: str) -> None:
    store = _store_factory(kind)()
    store.save(_metadata(attrs={"executor": 42}))

    loaded = store.load_by_status(GraphInstanceStatus.PENDING)

    assert len(loaded) == 1
    assert loaded[0].attrs == {"executor": 42}


def test_in_memory_update_attrs_merges_existing_keys() -> None:
    store = InMemoryGraphInstanceStore()
    store.save(_metadata(attrs={"executor": 42, "host": "worker-a"}))

    store.update_attrs(_GRAPH_INSTANCE_ID, {"host": "worker-b", "optional": None})

    loaded = store.load(_GRAPH_INSTANCE_ID)
    assert loaded is not None
    assert loaded.attrs == {"executor": 42, "host": "worker-b", "optional": None}


def test_sqlite_schema_adds_nullable_attrs_column_without_losing_rows() -> None:
    connection = _legacy_connection()

    store = SqliteGraphInstanceStore(connection)

    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(graph_instances)")
    }
    loaded = store.load(_GRAPH_INSTANCE_ID)
    assert "attrs_json" in columns
    assert loaded is not None
    assert loaded.attrs == {}


def test_sqlite_migrated_row_accepts_attrs_update() -> None:
    connection = _legacy_connection()
    store = SqliteGraphInstanceStore(connection)

    store.update_attrs(_GRAPH_INSTANCE_ID, {"executor": 42})

    loaded = store.load(_GRAPH_INSTANCE_ID)
    assert loaded is not None
    assert loaded.attrs == {"executor": 42}


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_update_attrs_merges_into_latest_without_changing_prior_version(
    kind: str,
) -> None:
    store, prior_version_is_preserved = _store_with_prior_version_check(kind)
    store.save(_metadata())
    store.update_attrs(_GRAPH_INSTANCE_ID, {"a": 1})
    store.begin_invocation(_GRAPH_INSTANCE_ID)

    store.update_attrs(_GRAPH_INSTANCE_ID, {"b": 2})

    latest = store.load(_GRAPH_INSTANCE_ID)
    assert latest is not None
    assert latest.attrs == {"a": 1, "b": 2}
    assert prior_version_is_preserved()
