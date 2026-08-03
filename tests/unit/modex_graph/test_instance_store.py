"""Tests for GraphInstanceStore ABC + InMemoryGraphInstanceStore +
SqliteGraphInstanceStore (P1C.6).

Covers:

- `GraphInstanceStore` ABC (rule 7: ABC, not Protocol): 6 abstract methods.
- `InMemoryGraphInstanceStore`: save (upsert), load_by_id, load_by_status,
  load_by_parent, update_status, delete.
- `SqliteGraphInstanceStore`: same CRUD + idempotent schema, timestamps
  epoch ms, table/column constants, indexes created, file-based
  persistence, field-by-column mapping, UPSERT via ON CONFLICT, status
  CHECK constraint.
- Cross-instance isolation.
- `update_status` on non-existent ID is a no-op.
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
    GraphInstanceStatus,
    GraphInstanceStore,
    GraphMetadata,
    InMemoryGraphInstanceStore,
    SqliteGraphInstanceStore,
)

# ── Test helpers ──────────────────────────────────────────────────────────

_SPEC_ID = 5001
_GRAPH_INSTANCE_ID = 1001
_OTHER_INSTANCE_ID = 2002
_PARENT_INSTANCE_ID = 3003


def _make_metadata(
    graph_instance_id: int = _GRAPH_INSTANCE_ID,
    spec_id: int = _SPEC_ID,
    parent_instance_id: int | None = None,
    parent_node: str | None = None,
    status: str = "running",
) -> GraphMetadata:
    return GraphMetadata(
        graph_instance_id=graph_instance_id,
        spec_id=spec_id,
        parent_instance_id=parent_instance_id,
        parent_node=parent_node,
        status=GraphInstanceStatus(status),
        instance_seq=0,
        iteration_count=0,
        activated_sources={},
        pending_dispatches={},
    )


def _store_factory(kind: str) -> Callable[[], GraphInstanceStore]:
    if kind == "memory":
        return lambda: InMemoryGraphInstanceStore()
    if kind == "sqlite":
        return lambda: SqliteGraphInstanceStore(":memory:")
    raise ValueError(f"unknown kind: {kind}")


STORE_KINDS = ["memory", "sqlite"]


# ── GraphInstanceStore ABC ────────────────────────────────────────────────


class TestGraphInstanceStoreABC:
    def test_is_abc(self) -> None:
        assert issubclass(GraphInstanceStore, ABC)

    def test_cannot_instantiate_directly(self) -> None:
        with pytest.raises(TypeError):
            GraphInstanceStore()  # type: ignore[abstract]

    def test_six_abstract_methods(self) -> None:
        expected = {
            "save",
            "load_by_id",
            "load_by_status",
            "load_by_parent",
            "update_status",
            "delete",
        }
        assert set(GraphInstanceStore.__abstractmethods__) == expected

    def test_in_memory_is_subclass(self) -> None:
        assert issubclass(InMemoryGraphInstanceStore, GraphInstanceStore)

    def test_sqlite_is_subclass(self) -> None:
        assert issubclass(SqliteGraphInstanceStore, GraphInstanceStore)

    def test_is_not_protocol(self) -> None:
        from typing import Protocol

        assert not issubclass(GraphInstanceStore, Protocol)

    def test_in_memory_no_abstract_methods(self) -> None:
        assert len(InMemoryGraphInstanceStore.__abstractmethods__) == 0

    def test_sqlite_no_abstract_methods(self) -> None:
        assert len(SqliteGraphInstanceStore.__abstractmethods__) == 0


# ── Parametrized CRUD tests ───────────────────────────────────────────────


@pytest.mark.parametrize("kind", STORE_KINDS)
class TestGraphInstanceStoreCRUD:
    def test_save_and_load_by_id(self, kind: str) -> None:
        store = _store_factory(kind)()
        inst = _make_metadata()
        store.save(inst)
        loaded = store.load_by_id(_GRAPH_INSTANCE_ID)
        assert loaded is not None
        assert loaded.graph_instance_id == _GRAPH_INSTANCE_ID
        assert loaded.spec_id == _SPEC_ID
        assert loaded.status == "running"

    def test_load_by_id_returns_none_for_missing(self, kind: str) -> None:
        store = _store_factory(kind)()
        assert store.load_by_id(99999) is None

    def test_save_is_upsert(self, kind: str) -> None:
        store = _store_factory(kind)()
        store.save(_make_metadata(status="running"))
        store.save(_make_metadata(status="completed"))
        loaded = store.load_by_id(_GRAPH_INSTANCE_ID)
        assert loaded is not None
        assert loaded.status == "completed"

    def test_load_by_status(self, kind: str) -> None:
        store = _store_factory(kind)()
        store.save(_make_metadata(graph_instance_id=1, status="running"))
        store.save(_make_metadata(graph_instance_id=2, status="crashed"))
        store.save(_make_metadata(graph_instance_id=3, status="running"))
        store.save(_make_metadata(graph_instance_id=4, status="completed"))
        crashed = store.load_by_status("crashed")
        assert len(crashed) == 1
        assert crashed[0].graph_instance_id == 2
        running = store.load_by_status("running")
        assert len(running) == 2
        running_ids = {i.graph_instance_id for i in running}
        assert running_ids == {1, 3}

    def test_load_by_status_empty(self, kind: str) -> None:
        store = _store_factory(kind)()
        assert store.load_by_status("crashed") == []

    def test_load_by_parent(self, kind: str) -> None:
        store = _store_factory(kind)()
        store.save(
            _make_metadata(
                graph_instance_id=1, parent_instance_id=_PARENT_INSTANCE_ID, parent_node="child_a"
            )
        )
        store.save(_make_metadata(graph_instance_id=2))
        store.save(
            _make_metadata(
                graph_instance_id=3, parent_instance_id=_PARENT_INSTANCE_ID, parent_node="child_b"
            )
        )
        children = store.load_by_parent(_PARENT_INSTANCE_ID)
        assert len(children) == 2
        child_ids = {c.graph_instance_id for c in children}
        assert child_ids == {1, 3}
        assert all(c.parent_instance_id == _PARENT_INSTANCE_ID for c in children)

    def test_load_by_parent_empty(self, kind: str) -> None:
        store = _store_factory(kind)()
        assert store.load_by_parent(99999) == []

    def test_update_status(self, kind: str) -> None:
        store = _store_factory(kind)()
        store.save(_make_metadata(status="running"))
        store.update_status(_GRAPH_INSTANCE_ID, "paused")
        loaded = store.load_by_id(_GRAPH_INSTANCE_ID)
        assert loaded is not None
        assert loaded.status == "paused"

    def test_update_status_all_lifecycle_transitions(self, kind: str) -> None:
        store = _store_factory(kind)()
        store.save(_make_metadata(status="running"))
        for status in ("paused", "stopped", "crashed", "completed", "failed"):
            store.update_status(_GRAPH_INSTANCE_ID, status)
            loaded = store.load_by_id(_GRAPH_INSTANCE_ID)
            assert loaded is not None
            assert loaded.status == status

    def test_update_status_nonexistent_is_noop(self, kind: str) -> None:
        store = _store_factory(kind)()
        store.update_status(99999, "crashed")

    def test_delete_removes_instance(self, kind: str) -> None:
        store = _store_factory(kind)()
        store.save(_make_metadata())
        store.delete(_GRAPH_INSTANCE_ID)
        assert store.load_by_id(_GRAPH_INSTANCE_ID) is None

    def test_delete_nonexistent_is_noop(self, kind: str) -> None:
        store = _store_factory(kind)()
        store.delete(99999)

    def test_different_instances_isolated(self, kind: str) -> None:
        store = _store_factory(kind)()
        store.save(_make_metadata(graph_instance_id=1, status="running"))
        store.save(_make_metadata(graph_instance_id=2, status="crashed"))
        assert store.load_by_id(1) is not None
        assert store.load_by_id(1).status == "running"  # type: ignore[union-attr]
        assert store.load_by_id(2) is not None
        assert store.load_by_id(2).status == "crashed"  # type: ignore[union-attr]

    def test_save_preserves_parent_linkage(self, kind: str) -> None:
        store = _store_factory(kind)()
        store.save(
            _make_metadata(
                parent_instance_id=_PARENT_INSTANCE_ID,
                parent_node="spawn_node",
            )
        )
        loaded = store.load_by_id(_GRAPH_INSTANCE_ID)
        assert loaded is not None
        assert loaded.parent_instance_id == _PARENT_INSTANCE_ID
        assert loaded.parent_node == "spawn_node"

    def test_save_preserves_null_parent(self, kind: str) -> None:
        store = _store_factory(kind)()
        store.save(_make_metadata(parent_instance_id=None, parent_node=None))
        loaded = store.load_by_id(_GRAPH_INSTANCE_ID)
        assert loaded is not None
        assert loaded.parent_instance_id is None
        assert loaded.parent_node is None

    def test_load_by_status_returns_correct_spec_id(self, kind: str) -> None:
        store = _store_factory(kind)()
        store.save(_make_metadata(graph_instance_id=1, spec_id=111, status="running"))
        store.save(_make_metadata(graph_instance_id=2, spec_id=222, status="running"))
        result = store.load_by_status("running")
        assert len(result) == 2
        spec_ids = {i.spec_id for i in result}
        assert spec_ids == {111, 222}


# ── SqliteGraphInstanceStore specifics ────────────────────────────────────


class TestSqliteGraphInstanceStoreSpecifics:
    def test_create_table_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "instances.db")
            store1 = SqliteGraphInstanceStore(db_path)
            store1.save(_make_metadata(status="running"))
            store1.close()
            store2 = SqliteGraphInstanceStore(db_path)
            loaded = store2.load_by_id(_GRAPH_INSTANCE_ID)
            assert loaded is not None
            assert loaded.status == "running"
            store2.close()

    def test_table_and_column_constants(self) -> None:
        from modex_graph.persistence.instance_store import (
            _COL_CREATED_AT,
            _COL_GRAPH_INSTANCE_ID,
            _COL_PARENT_INSTANCE_ID,
            _COL_PARENT_NODE,
            _COL_SPEC_ID,
            _COL_STATUS,
            _COL_UPDATED_AT,
            _INSTANCE_TABLE,
        )

        assert _INSTANCE_TABLE == "graph_instances"
        assert _COL_GRAPH_INSTANCE_ID == "graph_instance_id"
        assert _COL_SPEC_ID == "spec_id"
        assert _COL_PARENT_INSTANCE_ID == "parent_instance_id"
        assert _COL_PARENT_NODE == "parent_node"
        assert _COL_STATUS == "status"
        assert _COL_CREATED_AT == "created_at"
        assert _COL_UPDATED_AT == "updated_at"

    def test_indexes_created(self) -> None:
        store = SqliteGraphInstanceStore(":memory:")
        indexes = store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name = ?",
            ("graph_instances",),
        ).fetchall()
        index_names = {r[0] for r in indexes}
        assert "idx_graph_instances_spec" in index_names
        assert "idx_graph_instances_parent" in index_names
        assert "idx_graph_instances_active" in index_names
        store.close()

    def test_file_based_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "instances.db")
            store1 = SqliteGraphInstanceStore(db_path)
            store1.save(_make_metadata(graph_instance_id=42, status="crashed"))
            store1.close()
            store2 = SqliteGraphInstanceStore(db_path)
            loaded = store2.load_by_id(42)
            assert loaded is not None
            assert loaded.status == "crashed"
            store2.close()

    def test_timestamps_are_epoch_ms(self) -> None:
        from modex_graph.persistence.instance_store import _COL_CREATED_AT, _INSTANCE_TABLE

        store = SqliteGraphInstanceStore(":memory:")
        store.save(_make_metadata())
        row = store._conn.execute(f"SELECT {_COL_CREATED_AT} FROM {_INSTANCE_TABLE}").fetchone()
        assert row is not None
        ts = row[0]
        assert isinstance(ts, int)
        assert ts > 1_700_000_000_000
        store.close()

    def test_status_check_constraint_rejects_invalid(self) -> None:
        from modex_graph.persistence.instance_store import _INSTANCE_TABLE

        store = SqliteGraphInstanceStore(":memory:")
        with pytest.raises(sqlite3.IntegrityError):
            store._conn.execute(
                f"INSERT INTO {_INSTANCE_TABLE} "
                f"(graph_instance_id, spec_id, parent_instance_id, parent_node, "
                f"status, created_at, updated_at) "
                f"VALUES (999, 1, NULL, NULL, 'invalid_status', 0, 0)"
            )
        store.close()

    def test_update_status_sets_updated_at(self) -> None:
        from modex_graph.persistence.instance_store import (
            _COL_UPDATED_AT,
            _INSTANCE_TABLE,
        )

        store = SqliteGraphInstanceStore(":memory:")
        store.save(_make_metadata(status="running"))
        original_row = store._conn.execute(
            f"SELECT {_COL_UPDATED_AT} FROM {_INSTANCE_TABLE} WHERE graph_instance_id = ?",
            (_GRAPH_INSTANCE_ID,),
        ).fetchone()
        assert original_row is not None
        original_ts = original_row[0]
        store.update_status(_GRAPH_INSTANCE_ID, "paused")
        updated_row = store._conn.execute(
            f"SELECT {_COL_UPDATED_AT} FROM {_INSTANCE_TABLE} WHERE graph_instance_id = ?",
            (_GRAPH_INSTANCE_ID,),
        ).fetchone()
        assert updated_row is not None
        assert updated_row[0] >= original_ts
        store.close()

    def test_close_is_safe_multiple_times(self) -> None:
        store = SqliteGraphInstanceStore(":memory:")
        store.close()
        store.close()

    def test_upsert_via_on_conflict(self) -> None:
        store = SqliteGraphInstanceStore(":memory:")
        store.save(_make_metadata(status="running"))
        store.save(_make_metadata(status="completed"))
        store.save(_make_metadata(status="failed"))
        loaded = store.load_by_id(_GRAPH_INSTANCE_ID)
        assert loaded is not None
        assert loaded.status == "failed"
        rows = store._conn.execute(
            "SELECT COUNT(*) FROM graph_instances WHERE graph_instance_id = ?",
            (_GRAPH_INSTANCE_ID,),
        ).fetchone()
        assert rows[0] == 1
        store.close()


# ── InMemoryGraphInstanceStore specifics ──────────────────────────────────


class TestInMemoryGraphInstanceStoreSpecifics:
    def test_internal_dict_keyed_by_id(self) -> None:
        store = InMemoryGraphInstanceStore()
        store.save(_make_metadata())
        assert _GRAPH_INSTANCE_ID in store._instances

    def test_update_status_replaces_with_new_instance(self) -> None:
        store = InMemoryGraphInstanceStore()
        store.save(_make_metadata(status="running"))
        original = store._instances[_GRAPH_INSTANCE_ID]
        store.update_status(_GRAPH_INSTANCE_ID, "crashed")
        updated = store._instances[_GRAPH_INSTANCE_ID]
        assert updated.status == "crashed"
        assert original.status == "running"
        assert updated is not original

    def test_save_upsert_replaces(self) -> None:
        store = InMemoryGraphInstanceStore()
        store.save(_make_metadata(status="running"))
        store.save(_make_metadata(status="completed"))
        assert len(store._instances) == 1
        assert store._instances[_GRAPH_INSTANCE_ID].status == "completed"
