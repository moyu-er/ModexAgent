"""Tests for NodeStateStore ABC + InMemoryNodeStateStore + SqliteNodeStateStore (P1C.6).

Covers:

- `NodeStateStore` ABC (rule 7: ABC, not Protocol): 6 abstract methods.
- `InMemoryNodeStateStore`: save (append-only), load_latest, load_version,
  load_all_versions, list_nodes, clear. Uses `default_id_generator()` for
  Snowflake IDs.
- `SqliteNodeStateStore`: same CRUD + idempotent schema, timestamps epoch
  ms, table/column constants, indexes created, file-based persistence,
  `json.dumps` / `json.loads` round-trip, MVCC append-only (no UPDATE).
- Cross-instance isolation.
- `clear` on non-existent instance is a no-op.
- Version ordering (ASC / DESC / latest).
- Follows the EXACT pattern of `test_deliver_store.py`.
"""

from __future__ import annotations

import sqlite3
import tempfile
from abc import ABC
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from modex_graph import (
    InMemoryNodeStateStore,
    NodeStateStore,
    SqliteNodeStateStore,
)

# ── Test helpers ──────────────────────────────────────────────────────────

_GRAPH_INSTANCE_ID = 1001
_OTHER_INSTANCE_ID = 2002


def _make_state(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    state: dict[str, Any] = {"count": 0, "messages": []}
    if extra:
        state.update(extra)
    return state


def _store_factory(kind: str) -> Callable[[], NodeStateStore]:
    if kind == "memory":
        return lambda: InMemoryNodeStateStore()
    if kind == "sqlite":
        return lambda: SqliteNodeStateStore(":memory:")
    raise ValueError(f"unknown kind: {kind}")


STORE_KINDS = ["memory", "sqlite"]


# ── NodeStateStore ABC ────────────────────────────────────────────────────


class TestNodeStateStoreABC:
    def test_is_abc(self) -> None:
        assert issubclass(NodeStateStore, ABC)

    def test_cannot_instantiate_directly(self) -> None:
        with pytest.raises(TypeError):
            NodeStateStore()  # type: ignore[abstract]

    def test_six_abstract_methods(self) -> None:
        expected = {
            "save",
            "load_latest",
            "load_version",
            "load_all_versions",
            "list_nodes",
            "clear",
        }
        assert set(NodeStateStore.__abstractmethods__) == expected

    def test_in_memory_is_subclass(self) -> None:
        assert issubclass(InMemoryNodeStateStore, NodeStateStore)

    def test_sqlite_is_subclass(self) -> None:
        assert issubclass(SqliteNodeStateStore, NodeStateStore)

    def test_is_not_protocol(self) -> None:
        from typing import Protocol

        assert not issubclass(NodeStateStore, Protocol)

    def test_in_memory_no_abstract_methods(self) -> None:
        assert len(InMemoryNodeStateStore.__abstractmethods__) == 0

    def test_sqlite_no_abstract_methods(self) -> None:
        assert len(SqliteNodeStateStore.__abstractmethods__) == 0


# ── Parametrized CRUD tests ───────────────────────────────────────────────


@pytest.mark.parametrize("kind", STORE_KINDS)
class TestNodeStateStoreCRUD:
    def test_save_returns_int_node_state_id(self, kind: str) -> None:
        store = _store_factory(kind)()
        ns_id = store.save(_GRAPH_INSTANCE_ID, "node_a", 0, _make_state())
        assert isinstance(ns_id, int)
        assert ns_id > 0

    def test_save_generates_unique_ids(self, kind: str) -> None:
        store = _store_factory(kind)()
        id1 = store.save(_GRAPH_INSTANCE_ID, "a", 0, _make_state())
        id2 = store.save(_GRAPH_INSTANCE_ID, "a", 1, _make_state())
        assert id1 != id2

    def test_load_latest_returns_highest_version(self, kind: str) -> None:
        store = _store_factory(kind)()
        store.save(_GRAPH_INSTANCE_ID, "node_a", 0, {"count": 0})
        store.save(_GRAPH_INSTANCE_ID, "node_a", 1, {"count": 1})
        store.save(_GRAPH_INSTANCE_ID, "node_a", 2, {"count": 2})
        result = store.load_latest(_GRAPH_INSTANCE_ID, "node_a")
        assert result is not None
        state, version = result
        assert version == 2
        assert state["count"] == 2

    def test_load_latest_returns_none_for_missing(self, kind: str) -> None:
        store = _store_factory(kind)()
        assert store.load_latest(_GRAPH_INSTANCE_ID, "nonexistent") is None

    def test_load_latest_single_version(self, kind: str) -> None:
        store = _store_factory(kind)()
        store.save(_GRAPH_INSTANCE_ID, "node_a", 0, {"count": 42})
        result = store.load_latest(_GRAPH_INSTANCE_ID, "node_a")
        assert result is not None
        state, version = result
        assert version == 0
        assert state["count"] == 42

    def test_load_version_returns_specific_state(self, kind: str) -> None:
        store = _store_factory(kind)()
        store.save(_GRAPH_INSTANCE_ID, "node_a", 0, {"count": 0})
        store.save(_GRAPH_INSTANCE_ID, "node_a", 1, {"count": 1})
        store.save(_GRAPH_INSTANCE_ID, "node_a", 2, {"count": 2})
        state = store.load_version(_GRAPH_INSTANCE_ID, "node_a", 1)
        assert state is not None
        assert state["count"] == 1

    def test_load_version_returns_none_for_missing(self, kind: str) -> None:
        store = _store_factory(kind)()
        assert store.load_version(_GRAPH_INSTANCE_ID, "node_a", 99) is None

    def test_load_all_versions_ordered_asc(self, kind: str) -> None:
        store = _store_factory(kind)()
        store.save(_GRAPH_INSTANCE_ID, "node_a", 2, {"count": 2})
        store.save(_GRAPH_INSTANCE_ID, "node_a", 0, {"count": 0})
        store.save(_GRAPH_INSTANCE_ID, "node_a", 1, {"count": 1})
        versions = store.load_all_versions(_GRAPH_INSTANCE_ID, "node_a")
        assert len(versions) == 3
        version_nums = [v for (_, v) in versions]
        assert version_nums == [0, 1, 2]

    def test_load_all_versions_empty(self, kind: str) -> None:
        store = _store_factory(kind)()
        assert store.load_all_versions(_GRAPH_INSTANCE_ID, "node_a") == []

    def test_list_nodes_returns_distinct_names(self, kind: str) -> None:
        store = _store_factory(kind)()
        store.save(_GRAPH_INSTANCE_ID, "node_a", 0, _make_state())
        store.save(_GRAPH_INSTANCE_ID, "node_b", 0, _make_state())
        store.save(_GRAPH_INSTANCE_ID, "node_a", 1, _make_state())
        store.save(_GRAPH_INSTANCE_ID, "node_c", 0, _make_state())
        nodes = store.list_nodes(_GRAPH_INSTANCE_ID)
        assert len(nodes) == 3
        assert set(nodes) == {"node_a", "node_b", "node_c"}

    def test_list_nodes_empty(self, kind: str) -> None:
        store = _store_factory(kind)()
        assert store.list_nodes(_GRAPH_INSTANCE_ID) == []

    def test_clear_removes_all_for_instance(self, kind: str) -> None:
        store = _store_factory(kind)()
        store.save(_GRAPH_INSTANCE_ID, "node_a", 0, _make_state())
        store.save(_GRAPH_INSTANCE_ID, "node_b", 0, _make_state())
        store.clear(_GRAPH_INSTANCE_ID)
        assert store.list_nodes(_GRAPH_INSTANCE_ID) == []
        assert store.load_latest(_GRAPH_INSTANCE_ID, "node_a") is None

    def test_clear_only_affects_specified_instance(self, kind: str) -> None:
        store = _store_factory(kind)()
        store.save(_GRAPH_INSTANCE_ID, "node_a", 0, _make_state())
        store.save(_OTHER_INSTANCE_ID, "node_a", 0, _make_state())
        store.clear(_GRAPH_INSTANCE_ID)
        assert store.list_nodes(_GRAPH_INSTANCE_ID) == []
        assert len(store.list_nodes(_OTHER_INSTANCE_ID)) == 1

    def test_clear_nonexistent_is_noop(self, kind: str) -> None:
        store = _store_factory(kind)()
        store.clear(99999)

    def test_different_instances_isolated(self, kind: str) -> None:
        store = _store_factory(kind)()
        store.save(_GRAPH_INSTANCE_ID, "node_a", 0, {"count": 100})
        store.save(_OTHER_INSTANCE_ID, "node_a", 0, {"count": 200})
        s1 = store.load_latest(_GRAPH_INSTANCE_ID, "node_a")
        s2 = store.load_latest(_OTHER_INSTANCE_ID, "node_a")
        assert s1 is not None
        assert s2 is not None
        assert s1[0]["count"] == 100
        assert s2[0]["count"] == 200

    def test_state_round_trip_dict(self, kind: str) -> None:
        store = _store_factory(kind)()
        state = {"key": "value", "num": 42, "nested": {"inner": [1, 2, 3]}}
        store.save(_GRAPH_INSTANCE_ID, "node_a", 0, state)
        result = store.load_latest(_GRAPH_INSTANCE_ID, "node_a")
        assert result is not None
        assert result[0] == state

    def test_state_round_trip_empty_dict(self, kind: str) -> None:
        store = _store_factory(kind)()
        store.save(_GRAPH_INSTANCE_ID, "node_a", 0, {})
        result = store.load_latest(_GRAPH_INSTANCE_ID, "node_a")
        assert result is not None
        assert result[0] == {}

    def test_state_round_trip_with_list(self, kind: str) -> None:
        store = _store_factory(kind)()
        state = {"messages": ["msg1", "msg2", "msg3"]}
        store.save(_GRAPH_INSTANCE_ID, "node_a", 0, state)
        result = store.load_latest(_GRAPH_INSTANCE_ID, "node_a")
        assert result is not None
        assert result[0] == state

    def test_state_round_trip_with_none_values(self, kind: str) -> None:
        store = _store_factory(kind)()
        state = {"field": None, "other": 42}
        store.save(_GRAPH_INSTANCE_ID, "node_a", 0, state)
        result = store.load_latest(_GRAPH_INSTANCE_ID, "node_a")
        assert result is not None
        assert result[0] == state

    def test_append_only_multiple_saves_same_version_rejected(self, kind: str) -> None:
        store = _store_factory(kind)()
        store.save(_GRAPH_INSTANCE_ID, "node_a", 0, _make_state())
        with pytest.raises((ValueError, sqlite3.IntegrityError)):
            store.save(_GRAPH_INSTANCE_ID, "node_a", 0, _make_state())

    def test_multiple_nodes_per_instance(self, kind: str) -> None:
        store = _store_factory(kind)()
        for node_name in ("a", "b", "c", "d", "e"):
            store.save(_GRAPH_INSTANCE_ID, node_name, 0, {"name": node_name})
        nodes = store.list_nodes(_GRAPH_INSTANCE_ID)
        assert len(nodes) == 5
        for node_name in ("a", "b", "c", "d", "e"):
            result = store.load_latest(_GRAPH_INSTANCE_ID, node_name)
            assert result is not None
            assert result[0]["name"] == node_name


# ── SqliteNodeStateStore specifics ────────────────────────────────────────


class TestSqliteNodeStateStoreSpecifics:
    def test_create_table_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "node_states.db")
            store1 = SqliteNodeStateStore(db_path)
            store1.save(_GRAPH_INSTANCE_ID, "node_a", 0, {"count": 1})
            store1.close()
            store2 = SqliteNodeStateStore(db_path)
            result = store2.load_latest(_GRAPH_INSTANCE_ID, "node_a")
            assert result is not None
            assert result[0]["count"] == 1
            store2.close()

    def test_table_and_column_constants(self) -> None:
        from modex_graph.node_state_store import (
            _COL_CREATED_AT,
            _COL_GRAPH_INSTANCE_ID,
            _COL_NODE_NAME,
            _COL_NODE_STATE_ID,
            _COL_STATE_JSON,
            _COL_VERSION,
            _NODE_STATE_TABLE,
        )

        assert _NODE_STATE_TABLE == "node_states"
        assert _COL_NODE_STATE_ID == "node_state_id"
        assert _COL_GRAPH_INSTANCE_ID == "graph_instance_id"
        assert _COL_NODE_NAME == "node_name"
        assert _COL_VERSION == "version"
        assert _COL_STATE_JSON == "state_json"
        assert _COL_CREATED_AT == "created_at"

    def test_indexes_created(self) -> None:
        store = SqliteNodeStateStore(":memory:")
        indexes = store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name = ?",
            ("node_states",),
        ).fetchall()
        index_names = {r[0] for r in indexes}
        assert "idx_node_states_latest" in index_names
        assert "idx_node_states_node" in index_names
        store.close()

    def test_file_based_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "node_states.db")
            store1 = SqliteNodeStateStore(db_path)
            store1.save(_GRAPH_INSTANCE_ID, "node_a", 0, {"data": 42})
            store1.save(_GRAPH_INSTANCE_ID, "node_a", 1, {"data": 43})
            store1.close()
            store2 = SqliteNodeStateStore(db_path)
            result = store2.load_latest(_GRAPH_INSTANCE_ID, "node_a")
            assert result is not None
            assert result[0]["data"] == 43
            assert result[1] == 1
            store2.close()

    def test_timestamps_are_epoch_ms(self) -> None:
        from modex_graph.node_state_store import _COL_CREATED_AT, _NODE_STATE_TABLE

        store = SqliteNodeStateStore(":memory:")
        store.save(_GRAPH_INSTANCE_ID, "node_a", 0, _make_state())
        row = store._conn.execute(
            f"SELECT {_COL_CREATED_AT} FROM {_NODE_STATE_TABLE}"
        ).fetchone()
        assert row is not None
        ts = row[0]
        assert isinstance(ts, int)
        assert ts > 1_700_000_000_000
        store.close()

    def test_state_json_is_valid_json(self) -> None:
        import json

        from modex_graph.node_state_store import _COL_STATE_JSON, _NODE_STATE_TABLE

        store = SqliteNodeStateStore(":memory:")
        store.save(_GRAPH_INSTANCE_ID, "node_a", 0, {"key": "value"})
        row = store._conn.execute(
            f"SELECT {_COL_STATE_JSON} FROM {_NODE_STATE_TABLE}"
        ).fetchone()
        assert row is not None
        data = json.loads(row[0])
        assert data == {"key": "value"}
        store.close()

    def test_no_updated_at_column(self) -> None:
        store = SqliteNodeStateStore(":memory:")
        columns = store._conn.execute(
            "PRAGMA table_info(node_states)"
        ).fetchall()
        col_names = {c[1] for c in columns}
        assert "updated_at" not in col_names
        assert "created_at" in col_names
        store.close()

    def test_unique_constraint_version(self) -> None:
        store = SqliteNodeStateStore(":memory:")
        store.save(_GRAPH_INSTANCE_ID, "node_a", 0, _make_state())
        with pytest.raises(sqlite3.IntegrityError):
            store.save(_GRAPH_INSTANCE_ID, "node_a", 0, _make_state())
        store.close()

    def test_close_is_safe_multiple_times(self) -> None:
        store = SqliteNodeStateStore(":memory:")
        store.close()
        store.close()

    def test_append_only_no_update_sql(self) -> None:
        store = SqliteNodeStateStore(":memory:")
        store.save(_GRAPH_INSTANCE_ID, "node_a", 0, {"v": 0})
        store.save(_GRAPH_INSTANCE_ID, "node_a", 1, {"v": 1})
        store.save(_GRAPH_INSTANCE_ID, "node_a", 2, {"v": 2})
        count = store._conn.execute(
            "SELECT COUNT(*) FROM node_states "
            "WHERE graph_instance_id = ? AND node_name = ?",
            (_GRAPH_INSTANCE_ID, "node_a"),
        ).fetchone()
        assert count[0] == 3
        v0 = store.load_version(_GRAPH_INSTANCE_ID, "node_a", 0)
        assert v0 is not None
        assert v0["v"] == 0
        store.close()


# ── InMemoryNodeStateStore specifics ──────────────────────────────────────


class TestInMemoryNodeStateStoreSpecifics:
    def test_internal_dict_keyed_by_instance_id(self) -> None:
        store = InMemoryNodeStateStore()
        store.save(_GRAPH_INSTANCE_ID, "node_a", 0, _make_state())
        assert _GRAPH_INSTANCE_ID in store._records
        assert len(store._records[_GRAPH_INSTANCE_ID]) == 1

    def test_records_store_node_state_id(self) -> None:
        store = InMemoryNodeStateStore()
        ns_id = store.save(_GRAPH_INSTANCE_ID, "node_a", 0, _make_state())
        record = store._records[_GRAPH_INSTANCE_ID][0]
        assert record[0] == ns_id
        assert record[1] == "node_a"
        assert record[2] == 0

    def test_clear_removes_from_dict(self) -> None:
        store = InMemoryNodeStateStore()
        store.save(_GRAPH_INSTANCE_ID, "node_a", 0, _make_state())
        store.clear(_GRAPH_INSTANCE_ID)
        assert _GRAPH_INSTANCE_ID not in store._records
