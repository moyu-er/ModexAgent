"""Tests for GraphSpecStore ABC + InMemoryGraphSpecStore + SqliteGraphSpecStore (P1C.6).

Covers:

- `GraphSpecStore` ABC (rule 7: ABC, not Protocol): 5 abstract methods.
- `InMemoryGraphSpecStore`: save, load_by_id, load_by_name, list_all, delete.
  Uses `default_id_generator()` for Snowflake IDs.
- `SqliteGraphSpecStore`: same CRUD + idempotent schema, timestamps epoch
  ms, table/column constants, indexes created, file-based persistence,
  `GraphSpec.model_dump_json()` / `model_validate_json()` round-trip.
- Cross-spec isolation (different `spec_id`).
- `delete` on non-existent ID is a no-op.
- Follows the EXACT pattern of `test_deliver_store.py`.
"""

from __future__ import annotations

import sqlite3
import tempfile
from abc import ABC
from collections.abc import Callable
from pathlib import Path

import pytest

from modex_graph import (
    EdgeSpec,
    GraphNode,
    GraphSpec,
    GraphSpecStore,
    InMemoryGraphSpecStore,
    NodeSpec,
    SchedulerKind,
    SqliteGraphSpecStore,
)

# ── Test helpers ──────────────────────────────────────────────────────────


def _make_spec(
    name: str = "test_graph",
    version: str = "1.0",
) -> GraphSpec:
    return GraphSpec(
        name=name,
        nodes=[NodeSpec(name="entry", node_type="function")],
        edges=[EdgeSpec(source=GraphNode.START, target="entry")],
        state_class="counter_state",
        scheduler=SchedulerKind.LINEAR,
        version=version,
        max_iterations=25,
    )


def _store_factory(kind: str) -> Callable[[], GraphSpecStore]:
    if kind == "memory":
        return lambda: InMemoryGraphSpecStore()
    if kind == "sqlite":
        return lambda: SqliteGraphSpecStore(sqlite3.connect(":memory:"))
    raise ValueError(f"unknown kind: {kind}")


STORE_KINDS = ["memory", "sqlite"]


# ── GraphSpecStore ABC ────────────────────────────────────────────────────


class TestGraphSpecStoreABC:
    def test_is_abc(self) -> None:
        assert issubclass(GraphSpecStore, ABC)

    def test_cannot_instantiate_directly(self) -> None:
        with pytest.raises(TypeError):
            GraphSpecStore()  # type: ignore[abstract]

    def test_five_abstract_methods(self) -> None:
        expected = {"save", "load_by_id", "load_by_name", "list_all", "delete"}
        assert set(GraphSpecStore.__abstractmethods__) == expected

    def test_in_memory_is_subclass(self) -> None:
        assert issubclass(InMemoryGraphSpecStore, GraphSpecStore)

    def test_sqlite_is_subclass(self) -> None:
        assert issubclass(SqliteGraphSpecStore, GraphSpecStore)

    def test_is_not_protocol(self) -> None:
        from typing import Protocol

        assert not issubclass(GraphSpecStore, Protocol)

    def test_in_memory_no_abstract_methods(self) -> None:
        assert len(InMemoryGraphSpecStore.__abstractmethods__) == 0

    def test_sqlite_no_abstract_methods(self) -> None:
        assert len(SqliteGraphSpecStore.__abstractmethods__) == 0


# ── Parametrized CRUD tests ───────────────────────────────────────────────


@pytest.mark.parametrize("kind", STORE_KINDS)
class TestGraphSpecStoreCRUD:
    def test_save_returns_int_spec_id(self, kind: str) -> None:
        store = _store_factory(kind)()
        spec_id = store.save(_make_spec())
        assert isinstance(spec_id, int)
        assert spec_id > 0

    def test_save_with_explicit_spec_id(self, kind: str) -> None:
        store = _store_factory(kind)()
        spec_id = store.save(_make_spec(), spec_id=42)
        assert spec_id == 42
        loaded = store.load_by_id(42)
        assert loaded is not None
        assert loaded.name == "test_graph"

    def test_save_generates_unique_ids(self, kind: str) -> None:
        store = _store_factory(kind)()
        id1 = store.save(
            _make_spec(name="g1"),
        )
        id2 = store.save(
            _make_spec(name="g2"),
        )
        assert id1 != id2

    def test_load_by_id_returns_spec(self, kind: str) -> None:
        store = _store_factory(kind)()
        spec = _make_spec()
        spec_id = store.save(spec)
        loaded = store.load_by_id(spec_id)
        assert loaded is not None
        assert loaded.name == spec.name
        assert loaded.version == spec.version

    def test_load_by_id_returns_none_for_missing(self, kind: str) -> None:
        store = _store_factory(kind)()
        assert store.load_by_id(99999) is None

    def test_load_by_name_returns_spec(self, kind: str) -> None:
        store = _store_factory(kind)()
        store.save(_make_spec(name="my_graph", version="2.0"))
        loaded = store.load_by_name("my_graph", "2.0")
        assert loaded is not None
        assert loaded.name == "my_graph"
        assert loaded.version == "2.0"

    def test_load_by_name_default_version(self, kind: str) -> None:
        store = _store_factory(kind)()
        store.save(_make_spec(name="default_ver"))
        loaded = store.load_by_name("default_ver")
        assert loaded is not None
        assert loaded.version == "1.0"

    def test_load_by_name_returns_none_for_missing(self, kind: str) -> None:
        store = _store_factory(kind)()
        assert store.load_by_name("nonexistent") is None

    def test_list_all_returns_all_specs(self, kind: str) -> None:
        store = _store_factory(kind)()
        store.save(_make_spec(name="g1"))
        store.save(_make_spec(name="g2"))
        store.save(_make_spec(name="g3"))
        all_specs = store.list_all()
        assert len(all_specs) == 3
        names = {s.name for s in all_specs}
        assert names == {"g1", "g2", "g3"}

    def test_list_all_empty(self, kind: str) -> None:
        store = _store_factory(kind)()
        assert store.list_all() == []

    def test_delete_removes_spec(self, kind: str) -> None:
        store = _store_factory(kind)()
        spec_id = store.save(_make_spec(name="to_delete"))
        store.delete(spec_id)
        assert store.load_by_id(spec_id) is None

    def test_delete_nonexistent_is_noop(self, kind: str) -> None:
        store = _store_factory(kind)()
        store.delete(99999)

    def test_delete_removes_from_name_index(self, kind: str) -> None:
        store = _store_factory(kind)()
        spec_id = store.save(_make_spec(name="indexed"))
        store.delete(spec_id)
        assert store.load_by_name("indexed") is None

    def test_serialization_round_trip_full_spec(self, kind: str) -> None:
        store = _store_factory(kind)()
        spec = GraphSpec(
            name="complex",
            nodes=[
                NodeSpec(name="entry", node_type="function", config={"x": 1}),
                NodeSpec(name="llm", node_type="agent", config={"model": "gpt-4"}),
            ],
            edges=[
                EdgeSpec(source=GraphNode.START, target="entry"),
                EdgeSpec(source="entry", target="llm"),
                EdgeSpec(source="llm", target=GraphNode.END),
            ],
            state_class="counter_state",
            scheduler=SchedulerKind.PARALLEL,
            version="3.1.4",
            metadata={"author": "test", "tags": ["a", "b"]},
            max_iterations=50,
        )
        spec_id = store.save(spec)
        loaded = store.load_by_id(spec_id)
        assert loaded is not None
        assert loaded == spec

    def test_serialization_round_trip_state_class_name(self, kind: str) -> None:
        store = _store_factory(kind)()
        spec = GraphSpec(
            name="registered",
            nodes=[NodeSpec(name="entry", node_type="function")],
            edges=[EdgeSpec(source=GraphNode.START, target="entry")],
            state_class="my_registered_state",
        )
        spec_id = store.save(spec)
        loaded = store.load_by_id(spec_id)
        assert loaded is not None
        assert loaded.state_class == "my_registered_state"

    def test_different_specs_isolated(self, kind: str) -> None:
        store = _store_factory(kind)()
        id1 = store.save(_make_spec(name="g1", version="1.0"))
        id2 = store.save(_make_spec(name="g2", version="1.0"))
        s1 = store.load_by_id(id1)
        s2 = store.load_by_id(id2)
        assert s1 is not None
        assert s2 is not None
        assert s1.name == "g1"
        assert s2.name == "g2"

    def test_same_name_version_rejected(self, kind: str) -> None:
        store = _store_factory(kind)()
        store.save(_make_spec(name="dup", version="1.0"))
        with pytest.raises((ValueError, sqlite3.IntegrityError)):
            store.save(_make_spec(name="dup", version="1.0"))

    def test_same_name_different_version_ok(self, kind: str) -> None:
        store = _store_factory(kind)()
        store.save(_make_spec(name="multi", version="1.0"))
        store.save(_make_spec(name="multi", version="2.0"))
        v1 = store.load_by_name("multi", "1.0")
        v2 = store.load_by_name("multi", "2.0")
        assert v1 is not None
        assert v2 is not None
        assert v1.version == "1.0"
        assert v2.version == "2.0"


# ── SqliteGraphSpecStore specifics ────────────────────────────────────────


class TestSqliteGraphSpecStoreSpecifics:
    def test_create_table_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "specs.db")
            conn1 = sqlite3.connect(db_path)
            store1 = SqliteGraphSpecStore(conn1)
            spec_id = store1.save(_make_spec(name="persist"))
            conn1.close()
            conn2 = sqlite3.connect(db_path)
            store2 = SqliteGraphSpecStore(conn2)
            loaded = store2.load_by_id(spec_id)
            assert loaded is not None
            assert loaded.name == "persist"
            conn2.close()

    def test_table_and_column_constants(self) -> None:
        from modex_graph.spec_store import (
            _COL_CREATED_AT,
            _COL_NAME,
            _COL_SPEC_ID,
            _COL_SPEC_JSON,
            _COL_UPDATED_AT,
            _COL_VERSION,
            _SPEC_TABLE,
        )

        assert _SPEC_TABLE == "graph_specs"
        assert _COL_SPEC_ID == "spec_id"
        assert _COL_NAME == "name"
        assert _COL_VERSION == "version"
        assert _COL_SPEC_JSON == "spec_json"
        assert _COL_CREATED_AT == "created_at"
        assert _COL_UPDATED_AT == "updated_at"

    def test_indexes_created(self) -> None:
        conn = sqlite3.connect(":memory:")
        store = SqliteGraphSpecStore(conn)
        indexes = store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name = ?",
            ("graph_specs",),
        ).fetchall()
        index_names = {r[0] for r in indexes}
        assert "idx_graph_specs_name" in index_names
        conn.close()

    def test_file_based_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "specs.db")
            conn1 = sqlite3.connect(db_path)
            store1 = SqliteGraphSpecStore(conn1)
            spec_id = store1.save(_make_spec(name="file_test"))
            conn1.close()
            conn2 = sqlite3.connect(db_path)
            store2 = SqliteGraphSpecStore(conn2)
            loaded = store2.load_by_id(spec_id)
            assert loaded is not None
            assert loaded.name == "file_test"
            conn2.close()

    def test_timestamps_are_epoch_ms(self) -> None:
        from modex_graph.spec_store import _COL_CREATED_AT, _SPEC_TABLE

        conn = sqlite3.connect(":memory:")
        store = SqliteGraphSpecStore(conn)
        store.save(_make_spec())
        row = store._conn.execute(f"SELECT {_COL_CREATED_AT} FROM {_SPEC_TABLE}").fetchone()
        assert row is not None
        ts = row[0]
        assert isinstance(ts, int)
        assert ts > 1_700_000_000_000
        conn.close()

    def test_spec_json_is_valid_json(self) -> None:
        import json

        from modex_graph.spec_store import _COL_SPEC_JSON, _SPEC_TABLE

        conn = sqlite3.connect(":memory:")
        store = SqliteGraphSpecStore(conn)
        store.save(_make_spec(name="json_check"))
        row = store._conn.execute(f"SELECT {_COL_SPEC_JSON} FROM {_SPEC_TABLE}").fetchone()
        assert row is not None
        data = json.loads(row[0])
        assert data["name"] == "json_check"
        conn.close()

    def test_unique_name_version_constraint(self) -> None:
        conn = sqlite3.connect(":memory:")
        store = SqliteGraphSpecStore(conn)
        store.save(_make_spec(name="uniq", version="1.0"))
        with pytest.raises(sqlite3.IntegrityError):
            store.save(_make_spec(name="uniq", version="1.0"))
        conn.close()


# ── InMemoryGraphSpecStore specifics ──────────────────────────────────────


class TestInMemoryGraphSpecStoreSpecifics:
    def test_internal_dict_keyed_by_spec_id(self) -> None:
        store = InMemoryGraphSpecStore()
        spec_id = store.save(_make_spec(name="internal"))
        assert spec_id in store._specs
        assert store._specs[spec_id].name == "internal"

    def test_name_version_index_populated(self) -> None:
        store = InMemoryGraphSpecStore()
        spec_id = store.save(_make_spec(name="indexed", version="2.0"))
        assert store._by_name_version[("indexed", "2.0")] == spec_id

    def test_delete_cleans_name_index(self) -> None:
        store = InMemoryGraphSpecStore()
        spec_id = store.save(_make_spec(name="to_remove"))
        store.delete(spec_id)
        assert ("to_remove", "1.0") not in store._by_name_version
