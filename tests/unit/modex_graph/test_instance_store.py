"""Tests for GraphInstanceStore ABC + NullGraphInstanceStore +
InMemoryGraphInstanceStore + SqliteGraphInstanceStore.

Covers:

- `GraphInstanceStore` ABC (rule 7: ABC, not Protocol): 6 abstract methods.
- `NullGraphInstanceStore`: no-op; `load` returns None.
- `InMemoryGraphInstanceStore`: save (upsert), load, load_by_status,
  load_by_parent, update_status, delete.
- `SqliteGraphInstanceStore`: same CRUD + idempotent schema, timestamps
  epoch ms, table/column constants, indexes created, file-based
  persistence, field-by-column mapping for identity/status columns,
  UPSERT via ON CONFLICT, status CHECK constraint.
- Cross-instance isolation.
- `update_status` on non-existent ID is a no-op.
- `delete` on non-existent ID is a no-op.
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
    GraphIORecord,
    GraphMetadata,
    InMemoryGraphInstanceStore,
    NullGraphInstanceStore,
    SqliteGraphInstanceStore,
    SqliteGraphIORecordStore,
)

# ── Test helpers ──────────────────────────────────────────────────────────

_SPEC_ID = 5001
_GRAPH_INSTANCE_ID = 1001
_OTHER_INSTANCE_ID = 2002
_PARENT_INSTANCE_ID = 3003


def _make_metadata(
    graph_instance_id: int = _GRAPH_INSTANCE_ID,
    spec_id: int = _SPEC_ID,
    version: int = 0,
    parent_instance_id: int | None = None,
    parent_node: str | None = None,
    status: GraphInstanceStatus = GraphInstanceStatus.RUNNING,
) -> GraphMetadata:
    return GraphMetadata(
        graph_instance_id=graph_instance_id,
        spec_id=spec_id,
        version=version,
        parent_instance_id=parent_instance_id,
        parent_node=parent_node,
        status=status,
    )


def _store_factory(kind: str) -> Callable[[], GraphInstanceStore]:
    if kind == "null":
        return lambda: NullGraphInstanceStore()
    if kind == "memory":
        return lambda: InMemoryGraphInstanceStore()
    if kind == "sqlite":
        return lambda: SqliteGraphInstanceStore(sqlite3.connect(":memory:"))
    raise ValueError(f"unknown kind: {kind}")


STORE_KINDS = ["memory", "sqlite"]


# ── GraphInstanceStore ABC ────────────────────────────────────────────────


class TestGraphInstanceStoreABC:
    def test_is_abc(self) -> None:
        assert issubclass(GraphInstanceStore, ABC)

    def test_cannot_instantiate_directly(self) -> None:
        with pytest.raises(TypeError):
            GraphInstanceStore()  # type: ignore[abstract]

    def test_abstract_methods(self) -> None:
        expected = {
            "save",
            "load",
            "load_by_status",
            "load_by_parent",
            "update_attrs",
            "update_status",
            "delete",
            "begin_invocation",
            "complete_invocation",
            "suspend_invocation",
            "crash_invocation",
            "finalize_invocation",
        }
        assert set(GraphInstanceStore.__abstractmethods__) == expected

    def test_in_memory_is_subclass(self) -> None:
        assert issubclass(InMemoryGraphInstanceStore, GraphInstanceStore)

    def test_sqlite_is_subclass(self) -> None:
        assert issubclass(SqliteGraphInstanceStore, GraphInstanceStore)

    def test_null_is_subclass(self) -> None:
        assert issubclass(NullGraphInstanceStore, GraphInstanceStore)

    def test_is_not_protocol(self) -> None:
        from typing import Protocol

        assert not issubclass(GraphInstanceStore, Protocol)

    def test_in_memory_no_abstract_methods(self) -> None:
        assert len(InMemoryGraphInstanceStore.__abstractmethods__) == 0

    def test_sqlite_no_abstract_methods(self) -> None:
        assert len(SqliteGraphInstanceStore.__abstractmethods__) == 0

    def test_null_no_abstract_methods(self) -> None:
        assert len(NullGraphInstanceStore.__abstractmethods__) == 0


# ── NullGraphInstanceStore ─────────────────────────────────────────────────


class TestNullGraphInstanceStore:
    def test_load_returns_none(self) -> None:
        store = NullGraphInstanceStore()
        assert store.load(_GRAPH_INSTANCE_ID) is None

    def test_load_by_status_returns_empty(self) -> None:
        store = NullGraphInstanceStore()
        assert store.load_by_status(GraphInstanceStatus.CRASHED) == []

    def test_load_by_parent_returns_empty(self) -> None:
        store = NullGraphInstanceStore()
        assert store.load_by_parent(_PARENT_INSTANCE_ID) == []

    def test_save_is_noop(self) -> None:
        store = NullGraphInstanceStore()
        store.save(_make_metadata())
        assert store.load(_GRAPH_INSTANCE_ID) is None

    def test_update_status_is_noop(self) -> None:
        store = NullGraphInstanceStore()
        store.update_status(_GRAPH_INSTANCE_ID, GraphInstanceStatus.PAUSED)
        assert store.load(_GRAPH_INSTANCE_ID) is None

    def test_delete_is_noop(self) -> None:
        store = NullGraphInstanceStore()
        store.delete(_GRAPH_INSTANCE_ID)
        assert store.load(_GRAPH_INSTANCE_ID) is None


# ── Parametrized CRUD tests ───────────────────────────────────────────────


@pytest.mark.parametrize("kind", STORE_KINDS)
class TestGraphInstanceStoreCRUD:
    def test_save_and_load(self, kind: str) -> None:
        store = _store_factory(kind)()
        inst = _make_metadata()
        store.save(inst)
        loaded = store.load(_GRAPH_INSTANCE_ID)
        assert loaded is not None
        assert loaded.graph_instance_id == _GRAPH_INSTANCE_ID
        assert loaded.spec_id == _SPEC_ID
        assert loaded.status == GraphInstanceStatus.RUNNING

    def test_load_returns_none_for_missing(self, kind: str) -> None:
        store = _store_factory(kind)()
        assert store.load(99999) is None

    def test_save_inserts_version_rows(self, kind: str) -> None:
        store = _store_factory(kind)()
        store.save(_make_metadata(version=0, status=GraphInstanceStatus.RUNNING))
        store.save(_make_metadata(version=1, status=GraphInstanceStatus.COMPLETED))
        loaded = store.load(_GRAPH_INSTANCE_ID)
        assert loaded is not None
        assert loaded.version == 1
        assert loaded.status == GraphInstanceStatus.COMPLETED

    def test_load_by_status(self, kind: str) -> None:
        store = _store_factory(kind)()
        store.save(_make_metadata(graph_instance_id=1, status=GraphInstanceStatus.RUNNING))
        store.save(_make_metadata(graph_instance_id=2, status=GraphInstanceStatus.CRASHED))
        store.save(_make_metadata(graph_instance_id=3, status=GraphInstanceStatus.RUNNING))
        store.save(_make_metadata(graph_instance_id=4, status=GraphInstanceStatus.COMPLETED))
        crashed = store.load_by_status(GraphInstanceStatus.CRASHED)
        assert len(crashed) == 1
        assert crashed[0].graph_instance_id == 2
        running = store.load_by_status(GraphInstanceStatus.RUNNING)
        assert len(running) == 2
        running_ids = {i.graph_instance_id for i in running}
        assert running_ids == {1, 3}

    def test_load_by_status_empty(self, kind: str) -> None:
        store = _store_factory(kind)()
        assert store.load_by_status(GraphInstanceStatus.CRASHED) == []

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
        store.save(_make_metadata(status=GraphInstanceStatus.RUNNING))
        store.update_status(_GRAPH_INSTANCE_ID, GraphInstanceStatus.PAUSED)
        loaded = store.load(_GRAPH_INSTANCE_ID)
        assert loaded is not None
        assert loaded.status == GraphInstanceStatus.PAUSED

    def test_update_status_all_lifecycle_transitions(self, kind: str) -> None:
        store = _store_factory(kind)()
        store.save(_make_metadata(status=GraphInstanceStatus.RUNNING))
        for status in (
            GraphInstanceStatus.PAUSING,
            GraphInstanceStatus.PAUSED,
            GraphInstanceStatus.STOPPING,
            GraphInstanceStatus.STOPPED,
            GraphInstanceStatus.CRASHED,
            GraphInstanceStatus.COMPLETED,
            GraphInstanceStatus.FAILED,
        ):
            store.update_status(_GRAPH_INSTANCE_ID, status)
            loaded = store.load(_GRAPH_INSTANCE_ID)
            assert loaded is not None
            assert loaded.status == status

    def test_suspend_after_pause_request(self, kind: str) -> None:
        store = _store_factory(kind)()
        store.save(_make_metadata())
        invocation = store.begin_invocation(_GRAPH_INSTANCE_ID)
        store.update_status(_GRAPH_INSTANCE_ID, GraphInstanceStatus.PAUSING)
        store.suspend_invocation(invocation)
        loaded = store.load(_GRAPH_INSTANCE_ID)
        assert loaded is not None and loaded.status == GraphInstanceStatus.PAUSED
        store.finalize_invocation(invocation)
        loaded = store.load(_GRAPH_INSTANCE_ID)
        assert loaded is not None and loaded.status == GraphInstanceStatus.PAUSED

    @pytest.mark.parametrize("status", ["pausing", "stopping"])
    def test_finalize_abandoned_drain_is_crashed(self, kind: str, status: str) -> None:
        store = _store_factory(kind)()
        store.save(_make_metadata())
        invocation = store.begin_invocation(_GRAPH_INSTANCE_ID)
        store.update_status(_GRAPH_INSTANCE_ID, GraphInstanceStatus(status))
        store.finalize_invocation(invocation)
        loaded = store.load(_GRAPH_INSTANCE_ID)
        assert loaded is not None and loaded.status == GraphInstanceStatus.CRASHED

    def test_update_status_nonexistent_is_noop(self, kind: str) -> None:
        store = _store_factory(kind)()
        store.update_status(99999, GraphInstanceStatus.CRASHED)

    def test_delete_removes_instance(self, kind: str) -> None:
        store = _store_factory(kind)()
        store.save(_make_metadata())
        store.delete(_GRAPH_INSTANCE_ID)
        assert store.load(_GRAPH_INSTANCE_ID) is None

    def test_delete_nonexistent_is_noop(self, kind: str) -> None:
        store = _store_factory(kind)()
        store.delete(99999)

    def test_different_instances_isolated(self, kind: str) -> None:
        store = _store_factory(kind)()
        store.save(_make_metadata(graph_instance_id=1, status=GraphInstanceStatus.RUNNING))
        store.save(_make_metadata(graph_instance_id=2, status=GraphInstanceStatus.CRASHED))
        assert store.load(1) is not None
        assert store.load(1).status == GraphInstanceStatus.RUNNING  # type: ignore[union-attr]
        assert store.load(2) is not None
        assert store.load(2).status == GraphInstanceStatus.CRASHED  # type: ignore[union-attr]

    def test_save_preserves_parent_linkage(self, kind: str) -> None:
        store = _store_factory(kind)()
        store.save(
            _make_metadata(
                parent_instance_id=_PARENT_INSTANCE_ID,
                parent_node="spawn_node",
            )
        )
        loaded = store.load(_GRAPH_INSTANCE_ID)
        assert loaded is not None
        assert loaded.parent_instance_id == _PARENT_INSTANCE_ID
        assert loaded.parent_node == "spawn_node"

    def test_save_preserves_null_parent(self, kind: str) -> None:
        store = _store_factory(kind)()
        store.save(_make_metadata(parent_instance_id=None, parent_node=None))
        loaded = store.load(_GRAPH_INSTANCE_ID)
        assert loaded is not None
        assert loaded.parent_instance_id is None
        assert loaded.parent_node is None

    def test_load_by_status_returns_correct_spec_id(self, kind: str) -> None:
        store = _store_factory(kind)()
        store.save(_make_metadata(graph_instance_id=1, spec_id=111, status=GraphInstanceStatus.RUNNING))
        store.save(_make_metadata(graph_instance_id=2, spec_id=222, status=GraphInstanceStatus.RUNNING))
        result = store.load_by_status(GraphInstanceStatus.RUNNING)
        assert len(result) == 2
        spec_ids = {i.spec_id for i in result}
        assert spec_ids == {111, 222}


# ── SqliteGraphInstanceStore specifics ────────────────────────────────────


class TestSqliteGraphInstanceStoreSpecifics:
    @pytest.mark.parametrize("foreign_keys", [False, True])
    def test_existing_status_constraint_migrates_without_losing_versions(
        self, foreign_keys: bool,
    ) -> None:
        conn = sqlite3.connect(":memory:")
        conn.execute("""
            CREATE TABLE graph_instances (
                graph_instance_id INTEGER NOT NULL, spec_id INTEGER NOT NULL,
                version INTEGER NOT NULL DEFAULT 0,
                parent_instance_id INTEGER, parent_node TEXT,
                status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN
                    ('pending','running','paused','stopped','crashed','completed','failed')),
                node_id_map_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(node_id_map_json)),
                attrs_json TEXT, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
                PRIMARY KEY (graph_instance_id, version)
            )
        """)
        for version in (0, 1):
            conn.execute(
                "INSERT INTO graph_instances VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (42, 7, version, 11, "parent", "paused", '{"work":"node-1"}',
                 '{"executor_process_id":"test"}', 100, 200),
            )
        conn.commit()
        io_store = SqliteGraphIORecordStore(conn)
        io_record = GraphIORecord(
            record_id=91, graph_instance_id=42, spec_id=7, version=1, created_at=100,
        )
        io_store.save(io_record)
        conn.execute(f"PRAGMA foreign_keys = {int(foreign_keys)}")
        store = SqliteGraphInstanceStore(conn)
        assert io_store.get(91) == io_record
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == int(foreign_keys)
        for status in ("pausing", "stopping"):
            store.update_status(42, GraphInstanceStatus(status))
            assert store.load_by_status(GraphInstanceStatus(status))[0].version == 1
        latest = store.load(42)
        assert latest is not None
        assert latest.node_id_map == {"work": "node-1"}
        assert latest.attrs == {"executor_process_id": "test"}
        assert latest.parent_instance_id == 11 and latest.parent_node == "parent"
        assert latest.created_at == 100
        assert conn.execute(
            "SELECT status, created_at, updated_at FROM graph_instances WHERE version = 0"
        ).fetchone() == ("paused", 100, 200)
        SqliteGraphInstanceStore(conn)
        assert len(conn.execute("SELECT * FROM graph_instances").fetchall()) == 2
        conn.close()

    def test_node_id_map_round_trip(self) -> None:
        conn = sqlite3.connect(":memory:")
        store = SqliteGraphInstanceStore(conn)
        metadata = GraphMetadata(
            graph_instance_id=_GRAPH_INSTANCE_ID,
            spec_id=_SPEC_ID,
            parent_instance_id=None,
            parent_node=None,
            status=GraphInstanceStatus.RUNNING,
            node_id_map={"plan": "node-101", "execute": "node-202"},
        )

        store.save(metadata)

        loaded = store.load(_GRAPH_INSTANCE_ID)
        assert loaded is not None
        assert loaded.node_id_map == {"plan": "node-101", "execute": "node-202"}
        conn.close()

    def test_create_table_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "instances.db")
            conn1 = sqlite3.connect(db_path)
            store1 = SqliteGraphInstanceStore(conn1)
            store1.save(_make_metadata(status=GraphInstanceStatus.RUNNING))
            conn1.close()
            conn2 = sqlite3.connect(db_path)
            store2 = SqliteGraphInstanceStore(conn2)
            loaded = store2.load(_GRAPH_INSTANCE_ID)
            assert loaded is not None
            assert loaded.status == GraphInstanceStatus.RUNNING
            conn2.close()

    def test_table_and_column_constants(self) -> None:
        from modex_graph.persistence.instance_store import (
            _COL_CREATED_AT,
            _COL_GRAPH_INSTANCE_ID,
            _COL_NODE_ID_MAP_JSON,
            _COL_PARENT_INSTANCE_ID,
            _COL_PARENT_NODE,
            _COL_SPEC_ID,
            _COL_STATUS,
            _COL_UPDATED_AT,
            _INSTANCE_TABLE,
        )

        assert _INSTANCE_TABLE == "graph_instances"
        assert _COL_GRAPH_INSTANCE_ID == "graph_instance_id"
        assert _COL_NODE_ID_MAP_JSON == "node_id_map_json"
        assert _COL_SPEC_ID == "spec_id"
        assert _COL_PARENT_INSTANCE_ID == "parent_instance_id"
        assert _COL_PARENT_NODE == "parent_node"
        assert _COL_STATUS == "status"
        assert _COL_CREATED_AT == "created_at"
        assert _COL_UPDATED_AT == "updated_at"

    def test_indexes_created(self) -> None:
        conn = sqlite3.connect(":memory:")
        store = SqliteGraphInstanceStore(conn)
        indexes = store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name = ?",
            ("graph_instances",),
        ).fetchall()
        index_names = {r[0] for r in indexes}
        assert "idx_graph_instances_spec" in index_names
        assert "idx_graph_instances_parent" in index_names
        assert "idx_graph_instances_active" in index_names
        conn.close()

    def test_file_based_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "instances.db")
            conn1 = sqlite3.connect(db_path)
            store1 = SqliteGraphInstanceStore(conn1)
            store1.save(_make_metadata(graph_instance_id=42, status=GraphInstanceStatus.CRASHED))
            conn1.close()
            conn2 = sqlite3.connect(db_path)
            store2 = SqliteGraphInstanceStore(conn2)
            loaded = store2.load(42)
            assert loaded is not None
            assert loaded.status == GraphInstanceStatus.CRASHED
            conn2.close()

    def test_timestamps_are_epoch_ms(self) -> None:
        from modex_graph.persistence.instance_store import _COL_CREATED_AT, _INSTANCE_TABLE

        conn = sqlite3.connect(":memory:")
        store = SqliteGraphInstanceStore(conn)
        store.save(_make_metadata())
        row = store._conn.execute(f"SELECT {_COL_CREATED_AT} FROM {_INSTANCE_TABLE}").fetchone()
        assert row is not None
        ts = row[0]
        assert isinstance(ts, int)
        assert ts > 1_700_000_000_000
        conn.close()

    def test_save_load_populates_metadata_timestamps(self) -> None:
        conn = sqlite3.connect(":memory:")
        store = SqliteGraphInstanceStore(conn)
        store.save(_make_metadata(status=GraphInstanceStatus.RUNNING))
        loaded = store.load(_GRAPH_INSTANCE_ID)
        assert loaded is not None
        assert loaded.created_at > 0
        assert loaded.updated_at > 0
        conn.close()

    def test_load_by_status_populates_metadata_timestamps(self) -> None:
        conn = sqlite3.connect(":memory:")
        store = SqliteGraphInstanceStore(conn)
        store.save(_make_metadata(status=GraphInstanceStatus.RUNNING))
        loaded = store.load_by_status(GraphInstanceStatus.RUNNING)
        assert len(loaded) == 1
        assert loaded[0].created_at > 0
        assert loaded[0].updated_at > 0
        conn.close()

    def test_load_by_parent_populates_metadata_timestamps(self) -> None:
        conn = sqlite3.connect(":memory:")
        store = SqliteGraphInstanceStore(conn)
        store.save(
            _make_metadata(parent_instance_id=_PARENT_INSTANCE_ID, parent_node="child")
        )
        loaded = store.load_by_parent(_PARENT_INSTANCE_ID)
        assert len(loaded) == 1
        assert loaded[0].created_at > 0
        assert loaded[0].updated_at > 0
        conn.close()

    def test_status_check_constraint_rejects_invalid(self) -> None:
        from modex_graph.persistence.instance_store import _INSTANCE_TABLE

        conn = sqlite3.connect(":memory:")
        store = SqliteGraphInstanceStore(conn)
        with pytest.raises(sqlite3.IntegrityError):
            store._conn.execute(
                f"INSERT INTO {_INSTANCE_TABLE} "
                f"(graph_instance_id, spec_id, version, parent_instance_id, parent_node, "
                f"status, created_at, updated_at) "
                f"VALUES (999, 1, 0, NULL, NULL, 'invalid_status', 0, 0)"
            )
        conn.close()

    def test_update_status_sets_updated_at(self) -> None:
        from modex_graph.persistence.instance_store import (
            _COL_UPDATED_AT,
            _INSTANCE_TABLE,
        )

        conn = sqlite3.connect(":memory:")
        store = SqliteGraphInstanceStore(conn)
        store.save(_make_metadata(status=GraphInstanceStatus.RUNNING))
        original_row = store._conn.execute(
            f"SELECT {_COL_UPDATED_AT} FROM {_INSTANCE_TABLE} WHERE graph_instance_id = ?",
            (_GRAPH_INSTANCE_ID,),
        ).fetchone()
        assert original_row is not None
        original_ts = original_row[0]
        store.update_status(_GRAPH_INSTANCE_ID, GraphInstanceStatus.PAUSED)
        updated_row = store._conn.execute(
            f"SELECT {_COL_UPDATED_AT} FROM {_INSTANCE_TABLE} WHERE graph_instance_id = ?",
            (_GRAPH_INSTANCE_ID,),
        ).fetchone()
        assert updated_row is not None
        assert updated_row[0] >= original_ts
        conn.close()

    def test_insert_creates_version_chain(self) -> None:
        conn = sqlite3.connect(":memory:")
        store = SqliteGraphInstanceStore(conn)
        store.save(_make_metadata(version=0, status=GraphInstanceStatus.RUNNING))
        store.save(_make_metadata(version=1, status=GraphInstanceStatus.COMPLETED))
        store.save(_make_metadata(version=2, status=GraphInstanceStatus.FAILED))
        loaded = store.load(_GRAPH_INSTANCE_ID)
        assert loaded is not None
        assert loaded.version == 2
        assert loaded.status == GraphInstanceStatus.FAILED
        rows = store._conn.execute(
            "SELECT COUNT(*) FROM graph_instances WHERE graph_instance_id = ?",
            (_GRAPH_INSTANCE_ID,),
        ).fetchone()
        assert rows[0] == 3
        conn.close()


# ── InMemoryGraphInstanceStore specifics ──────────────────────────────────


class TestInMemoryGraphInstanceStoreSpecifics:
    def test_internal_dict_keyed_by_id(self) -> None:
        store = InMemoryGraphInstanceStore()
        store.save(_make_metadata())
        assert _GRAPH_INSTANCE_ID in store._instances

    def test_update_status_replaces_latest_version(self) -> None:
        store = InMemoryGraphInstanceStore()
        store.save(_make_metadata(version=0, status=GraphInstanceStatus.RUNNING))
        original = store._instances[_GRAPH_INSTANCE_ID][-1]
        store.update_status(_GRAPH_INSTANCE_ID, GraphInstanceStatus.CRASHED)
        updated = store._instances[_GRAPH_INSTANCE_ID][-1]
        assert updated.status == GraphInstanceStatus.CRASHED
        assert original.status == GraphInstanceStatus.RUNNING
        assert updated is not original

    def test_save_appends_version_rows(self) -> None:
        store = InMemoryGraphInstanceStore()
        store.save(_make_metadata(version=0, status=GraphInstanceStatus.RUNNING))
        store.save(_make_metadata(version=1, status=GraphInstanceStatus.COMPLETED))
        assert len(store._instances[_GRAPH_INSTANCE_ID]) == 2
        assert store._instances[_GRAPH_INSTANCE_ID][-1].status == GraphInstanceStatus.COMPLETED
