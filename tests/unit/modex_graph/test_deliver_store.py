# ruff: noqa: ANN401

"""Tests for DeliverStore ABC + InMemoryDeliverStore + SqliteDeliverStore.

Covers the active consumption API, frozen deliver records, in-memory and
SQLite state transitions, schema constraints, persistence, and isolation.
"""

from __future__ import annotations

import sqlite3
import tempfile
from abc import ABC
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import ValidationError

from modex_graph import (
    DeliverConsumptionStatus,
    DeliverRecord,
    DeliverStore,
    InMemoryDeliverStore,
    SqliteDeliverStore,
)

# ── Test helpers ──────────────────────────────────────────────────────────

_GRAPH_INSTANCE_ID = 1001
_OTHER_GRAPH_INSTANCE_ID = 2002
_SOURCE_NODE_ID = "node-source-001"
_SOURCE_INVOCATION_ID = 0


def _store_factory(kind: str) -> Callable[[], DeliverStore]:
    if kind == "memory":
        return lambda: InMemoryDeliverStore()
    if kind == "sqlite":
        return lambda: SqliteDeliverStore(sqlite3.connect(":memory:"))
    raise ValueError(f"unknown kind: {kind}")


def _accumulate(
    store: DeliverStore,
    *,
    graph_instance_id: int = _GRAPH_INSTANCE_ID,
    node_id: str = "node-a-001",
    content: Any = "data",
) -> int:
    """Wrap accumulate with default source values."""
    return store.accumulate(
        graph_instance_id=graph_instance_id,
        node_id=node_id,
        source_node_id=_SOURCE_NODE_ID,
        source_invocation_id=_SOURCE_INVOCATION_ID,
        content=content,
    )


STORE_KINDS = ["memory", "sqlite"]


# ── DeliverConsumptionStatus enum (used by DeliverRecord) ─────


class TestDeliverConsumptionStatus:
    def test_pending_value(self) -> None:
        assert DeliverConsumptionStatus.PENDING.value == "pending"

    def test_consumed_value(self) -> None:
        assert DeliverConsumptionStatus.CONSUMED.value == "consumed"

    def test_is_str_enum(self) -> None:
        from enum import StrEnum

        assert issubclass(DeliverConsumptionStatus, StrEnum)


# ── DeliverRecord ─────────────────────────────────────────────────────────


class TestDeliverRecord:
    def test_construction_with_all_fields(self) -> None:
        r = DeliverRecord(
            deliver_id=123,
            graph_instance_id=_GRAPH_INSTANCE_ID,
            node_id="node-a-001",
            source_node_id="node-source-001",
            source_invocation_id=99,
            consumed_by_invocation_id=None,
            content={"key": "value"},
            status=DeliverConsumptionStatus.PENDING,
            created_at=1700000000000,
            updated_at=1700000000000,
        )
        assert r.deliver_id == 123
        assert r.node_id == "node-a-001"
        assert r.source_node_id == "node-source-001"
        assert r.source_invocation_id == 99
        assert r.consumed_by_invocation_id is None
        assert r.content == {"key": "value"}

    def test_status_defaults_to_pending(self) -> None:
        r = DeliverRecord(
            deliver_id=1,
            graph_instance_id=_GRAPH_INSTANCE_ID,
            node_id="node-a-001",
            source_node_id="node-source-001",
            source_invocation_id=0,
            content="data",
            created_at=0,
            updated_at=0,
        )
        assert r.status == DeliverConsumptionStatus.PENDING

    def test_consumed_by_invocation_id_defaults_to_none(self) -> None:
        r = DeliverRecord(
            deliver_id=1,
            graph_instance_id=_GRAPH_INSTANCE_ID,
            node_id="node-a-001",
            source_node_id="node-source-001",
            source_invocation_id=0,
            content="data",
            created_at=0,
            updated_at=0,
        )
        assert r.consumed_by_invocation_id is None

    def test_frozen_cannot_set_fields(self) -> None:
        r = DeliverRecord(
            deliver_id=1,
            graph_instance_id=_GRAPH_INSTANCE_ID,
            node_id="node-a-001",
            source_node_id="node-source-001",
            source_invocation_id=0,
            content="data",
            created_at=0,
            updated_at=0,
        )
        with pytest.raises(ValidationError):
            r.content = "new"  # type: ignore[misc]

    def test_extra_forbid_rejects_unknown_fields(self) -> None:
        with pytest.raises(ValidationError):
            DeliverRecord.model_validate(
                {
                    "deliver_id": 1,
                    "graph_instance_id": _GRAPH_INSTANCE_ID,
                    "node_id": "node-a-001",
                    "source_node_id": "node-source-001",
                    "source_invocation_id": 0,
                    "content": "data",
                    "created_at": 0,
                    "updated_at": 0,
                    "unknown": "bad",
                }
            )

    def test_content_can_be_any_json_serializable(self) -> None:
        for content in ["str", 42, [1, 2], {"k": "v"}, None, True]:
            r = DeliverRecord(
                deliver_id=1,
                graph_instance_id=_GRAPH_INSTANCE_ID,
                node_id="node-a-001",
                source_node_id="node-source-001",
                source_invocation_id=0,
                content=content,
                created_at=0,
                updated_at=0,
            )
            assert r.content == content

    def test_source_node_id_is_required(self) -> None:
        with pytest.raises(ValidationError):
            DeliverRecord.model_validate(
                {
                    "deliver_id": 1,
                    "graph_instance_id": _GRAPH_INSTANCE_ID,
                    "node_id": "node-a-001",
                    "source_invocation_id": 0,
                    "content": "data",
                    "created_at": 0,
                    "updated_at": 0,
                }
            )

    def test_source_invocation_id_is_required(self) -> None:
        with pytest.raises(ValidationError):
            DeliverRecord.model_validate(
                {
                    "deliver_id": 1,
                    "graph_instance_id": _GRAPH_INSTANCE_ID,
                    "node_id": "node-a-001",
                    "source_node_id": "node-source-001",
                    "content": "data",
                    "created_at": 0,
                    "updated_at": 0,
                }
            )


# ── DeliverStore ABC ──────────────────────────────────────────────────────


class TestDeliverStoreABC:
    def test_is_abc(self) -> None:
        assert issubclass(DeliverStore, ABC)

    def test_cannot_instantiate_directly(self) -> None:
        with pytest.raises(TypeError):
            DeliverStore()  # type: ignore[abstract]

    def test_four_abstract_methods(self) -> None:
        expected = {
            "accumulate",
            "query_consumable",
            "mark_consumed",
            "promote_consumed",
        }
        assert set(DeliverStore.__abstractmethods__) == expected

    def test_in_memory_is_subclass(self) -> None:
        assert issubclass(InMemoryDeliverStore, DeliverStore)

    def test_sqlite_is_subclass(self) -> None:
        assert issubclass(SqliteDeliverStore, DeliverStore)

    def test_is_not_protocol(self) -> None:
        from typing import Protocol

        assert not issubclass(DeliverStore, Protocol)

    def test_in_memory_no_abstract_methods(self) -> None:
        assert len(InMemoryDeliverStore.__abstractmethods__) == 0

    def test_sqlite_no_abstract_methods(self) -> None:
        assert len(SqliteDeliverStore.__abstractmethods__) == 0


# ── Consumption state machines ───────────────────────────────────────────


@pytest.mark.parametrize("kind", STORE_KINDS)
class TestDeliverStoreConsumption:
    def test_query_consumable_returns_pending(self, kind: str) -> None:
        store = _store_factory(kind)()
        _accumulate(store, node_id="node-a-001", content="data")
        assert [
            record.content
            for record in store.query_consumable(_GRAPH_INSTANCE_ID, "node-a-001")
        ] == [
            "data"
        ]

    def test_mark_consumed_records_consumer(self, kind: str) -> None:
        store = _store_factory(kind)()
        deliver_id = _accumulate(store, node_id="node-a-001", content="data")
        store.mark_consumed([deliver_id], 1)

        records = (
            cast(InMemoryDeliverStore, store)._records[_GRAPH_INSTANCE_ID]
            if kind == "memory"
            else store.query_consumable(_GRAPH_INSTANCE_ID, "node-a-001")
        )
        assert records[0].consumed_by_invocation_id == 1

    def test_promote_consumed_finishes_strategy_transition(self, kind: str) -> None:
        store = _store_factory(kind)()
        deliver_id = _accumulate(store, node_id="node-a-001", content="data")
        store.mark_consumed([deliver_id], 1)
        store.promote_consumed(1)

        if kind == "memory":
            assert cast(InMemoryDeliverStore, store)._records[_GRAPH_INSTANCE_ID] == []
        else:
            row = cast(SqliteDeliverStore, store)._conn.execute(
                "SELECT status FROM deliver_states WHERE deliver_id = ?", (deliver_id,)
            ).fetchone()
            assert row == (DeliverConsumptionStatus.CONSUMED_COMPLETED.value,)


# ── Parametrized accumulate/query/mark/clear tests ────────────────────────


@pytest.mark.parametrize("kind", STORE_KINDS)
class TestDeliverStoreCRUD:
    def test_accumulate_returns_int_deliver_id(self, kind: str) -> None:
        store = _store_factory(kind)()
        deliver_id = _accumulate(store, node_id="node-a-001", content={"data": 1})
        assert isinstance(deliver_id, int)
        assert deliver_id > 0

    def test_accumulate_generates_unique_ids(self, kind: str) -> None:
        store = _store_factory(kind)()
        id1 = _accumulate(store, node_id="node-a-001", content="data1")
        id2 = _accumulate(store, node_id="node-a-001", content="data2")
        assert id1 != id2

    def test_accumulate_is_keyword_only(self, kind: str) -> None:
        store = _store_factory(kind)()
        with pytest.raises(TypeError):
            store.accumulate(_GRAPH_INSTANCE_ID, "a", "src", 0, "data")  # type: ignore[call-arg]

    def test_query_consumable_returns_pending(self, kind: str) -> None:
        store = _store_factory(kind)()
        _accumulate(store, node_id="node-a-001", content="data1")
        _accumulate(store, node_id="node-a-001", content="data2")
        result = store.query_consumable(_GRAPH_INSTANCE_ID, "node-a-001")
        assert len(result) == 2
        assert all(r.node_id == "node-a-001" for r in result)
        assert all(r.status == DeliverConsumptionStatus.PENDING for r in result)

    def test_query_consumable_filters_by_node(self, kind: str) -> None:
        store = _store_factory(kind)()
        _accumulate(store, node_id="node-a-001", content="data")
        _accumulate(store, node_id="node-b-002", content="data")
        result = store.query_consumable(_GRAPH_INSTANCE_ID, "node-a-001")
        assert len(result) == 1
        assert result[0].node_id == "node-a-001"

    def test_different_graph_instances_isolated(self, kind: str) -> None:
        store = _store_factory(kind)()
        _accumulate(store, node_id="node-a-001", content="data1")
        _accumulate(
            store,
            graph_instance_id=_OTHER_GRAPH_INSTANCE_ID,
            node_id="node-a-001",
            content="data2",
        )
        assert len(store.query_consumable(_GRAPH_INSTANCE_ID, "node-a-001")) == 1
        assert len(store.query_consumable(_OTHER_GRAPH_INSTANCE_ID, "node-a-001")) == 1

    def test_query_empty_instance_returns_empty(self, kind: str) -> None:
        store = _store_factory(kind)()
        assert store.query_consumable(99999, "a") == []

    def test_content_round_trip_dict(self, kind: str) -> None:
        store = _store_factory(kind)()
        content = {"key": "value", "num": 42, "nested": {"inner": [1, 2, 3]}}
        _accumulate(store, node_id="node-a-001", content=content)
        result = store.query_consumable(_GRAPH_INSTANCE_ID, "node-a-001")
        assert result[0].content == content

    def test_content_round_trip_string(self, kind: str) -> None:
        store = _store_factory(kind)()
        _accumulate(store, node_id="node-a-001", content="hello")
        result = store.query_consumable(_GRAPH_INSTANCE_ID, "node-a-001")
        assert result[0].content == "hello"

    def test_content_round_trip_none(self, kind: str) -> None:
        store = _store_factory(kind)()
        _accumulate(store, node_id="node-a-001", content=None)
        result = store.query_consumable(_GRAPH_INSTANCE_ID, "node-a-001")
        assert result[0].content is None

    def test_content_round_trip_list(self, kind: str) -> None:
        store = _store_factory(kind)()
        _accumulate(store, node_id="node-a-001", content=[1, 2, 3])
        result = store.query_consumable(_GRAPH_INSTANCE_ID, "node-a-001")
        assert result[0].content == [1, 2, 3]

    def test_graph_payload_round_trip(self, kind: str) -> None:
        from modex_graph import GraphPayload

        store = _store_factory(kind)()
        payload = GraphPayload(content="hello from start node")
        _accumulate(store, node_id="node-a-001", content=payload)
        result = store.query_consumable(_GRAPH_INSTANCE_ID, "node-a-001")
        assert len(result) == 1
        assert isinstance(result[0].content, GraphPayload)
        assert result[0].content.content == "hello from start node"

    def test_preserves_insertion_order(self, kind: str) -> None:
        store = _store_factory(kind)()
        _accumulate(store, node_id="node-a-001", content="first")
        _accumulate(store, node_id="node-a-001", content="second")
        _accumulate(store, node_id="node-a-001", content="third")
        result = store.query_consumable(_GRAPH_INSTANCE_ID, "node-a-001")
        assert result[0].content == "first"
        assert result[1].content == "second"
        assert result[2].content == "third"

    def test_deliver_record_fields_populated(self, kind: str) -> None:
        store = _store_factory(kind)()
        deliver_id = _accumulate(store, node_id="node-a-001", content={"data": 1})
        result = store.query_consumable(_GRAPH_INSTANCE_ID, "node-a-001")
        assert len(result) == 1
        r = result[0]
        assert r.deliver_id == deliver_id
        assert r.graph_instance_id == _GRAPH_INSTANCE_ID
        assert r.node_id == "node-a-001"
        assert r.source_node_id == _SOURCE_NODE_ID
        assert r.source_invocation_id == _SOURCE_INVOCATION_ID
        assert r.consumed_by_invocation_id is None
        assert r.status == DeliverConsumptionStatus.PENDING
        assert isinstance(r.created_at, int)
        assert isinstance(r.updated_at, int)
        assert r.created_at > 1_700_000_000_000
        assert r.updated_at >= r.created_at

# ── SqliteDeliverStore specifics ──────────────────────────────────────────


class TestSqliteDeliverStoreSpecifics:
    def test_create_table_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "deliver.db")
            conn1 = sqlite3.connect(db_path)
            store1 = SqliteDeliverStore(conn1)
            store1.accumulate(
                graph_instance_id=_GRAPH_INSTANCE_ID,
                node_id="node-a-001",
                source_node_id="node-source-001",
                source_invocation_id=0,
                content={"k": "v"},
            )
            conn1.close()
            conn2 = sqlite3.connect(db_path)
            store2 = SqliteDeliverStore(conn2)
            result = store2.query_consumable(_GRAPH_INSTANCE_ID, "node-a-001")
            assert len(result) == 1
            assert result[0].content == {"k": "v"}
            conn2.close()

    def test_table_and_column_constants(self) -> None:
        from modex_graph.persistence.deliver_store import (
            _COL_CONSUMED_BY_INVOCATION_ID,
            _COL_CONTENT_JSON,
            _COL_CREATED_AT,
            _COL_DELIVER_ID,
            _COL_GRAPH_INSTANCE_ID,
            _COL_NEXT_NODE_ID,
            _COL_NODE_ID,
            _COL_SOURCE_INVOCATION_ID,
            _COL_SOURCE_NODE_ID,
            _COL_STATUS,
            _COL_UPDATED_AT,
            _DELIVER_TABLE,
        )

        assert _DELIVER_TABLE == "deliver_states"
        assert _COL_DELIVER_ID == "deliver_id"
        assert _COL_GRAPH_INSTANCE_ID == "graph_instance_id"
        assert _COL_NODE_ID == "node_id"
        assert _COL_NEXT_NODE_ID == "next_node_id"
        assert _COL_SOURCE_NODE_ID == "source_node_id"
        assert _COL_SOURCE_INVOCATION_ID == "source_invocation_id"
        assert _COL_CONSUMED_BY_INVOCATION_ID == "consumed_by_invocation_id"
        assert _COL_CONTENT_JSON == "content_json"
        assert _COL_STATUS == "status"
        assert _COL_CREATED_AT == "created_at"
        assert _COL_UPDATED_AT == "updated_at"

    def test_indexes_created(self) -> None:
        conn = sqlite3.connect(":memory:")
        store = SqliteDeliverStore(conn)
        indexes = store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name = ?",
            ("deliver_states",),
        ).fetchall()
        index_names = {r[0] for r in indexes}
        assert "idx_deliver_states_node" in index_names
        assert "idx_deliver_states_target" in index_names
        conn.close()

    def test_file_based_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "deliver.db")
            conn1 = sqlite3.connect(db_path)
            store1 = SqliteDeliverStore(conn1)
            store1.accumulate(
                graph_instance_id=_GRAPH_INSTANCE_ID,
                node_id="node-a-001",
                source_node_id="node-source-001",
                source_invocation_id=0,
                content={"data": 42},
            )
            conn1.close()
            conn2 = sqlite3.connect(db_path)
            store2 = SqliteDeliverStore(conn2)
            result = store2.query_consumable(_GRAPH_INSTANCE_ID, "node-a-001")
            assert len(result) == 1
            assert result[0].content == {"data": 42}
            conn2.close()

    def test_timestamps_are_epoch_ms(self) -> None:
        from modex_graph.persistence.deliver_store import _COL_CREATED_AT, _DELIVER_TABLE

        conn = sqlite3.connect(":memory:")
        store = SqliteDeliverStore(conn)
        store.accumulate(
            graph_instance_id=_GRAPH_INSTANCE_ID,
            node_id="node-a-001",
            source_node_id="node-source-001",
            source_invocation_id=0,
            content="data",
        )
        row = store._conn.execute(f"SELECT {_COL_CREATED_AT} FROM {_DELIVER_TABLE}").fetchone()
        assert row is not None
        ts = row[0]
        assert isinstance(ts, int)
        assert ts > 1_700_000_000_000
        conn.close()

    def test_status_check_constraint_rejects_invalid(self) -> None:
        from modex_graph.persistence.deliver_store import _DELIVER_TABLE

        conn = sqlite3.connect(":memory:")
        store = SqliteDeliverStore(conn)
        with pytest.raises(sqlite3_integrity_error()):
            store._conn.execute(
                f"INSERT INTO {_DELIVER_TABLE} (deliver_id, graph_instance_id, "
                f"node_id, next_node_id, content_json, status, created_at, updated_at) "
                f"VALUES (999, 1, 'node-a-001', 'node-b-002', '\"x\"', 'invalid_status', 0, 0)"
            )
        conn.close()

    def test_status_check_constraint_accepts_consumption_values(self) -> None:
        from modex_graph.persistence.deliver_store import _DELIVER_TABLE

        conn = sqlite3.connect(":memory:")
        store = SqliteDeliverStore(conn)
        for status_val in (
            DeliverConsumptionStatus.PENDING.value,
            DeliverConsumptionStatus.CONSUMED.value,
            DeliverConsumptionStatus.CONSUMED_PENDING.value,
            DeliverConsumptionStatus.CONSUMED_COMPLETED.value,
        ):
            store._conn.execute(
                f"INSERT INTO {_DELIVER_TABLE} (deliver_id, graph_instance_id, "
                f"node_id, next_node_id, content_json, status, created_at, updated_at) "
                f"VALUES (?, 1, 'node-a-001', 'node-b-002', '\"x\"', ?, 0, 0)",
                (hash(status_val) % 100000, status_val),
            )
        conn.close()

    def test_new_columns_exist_after_init(self) -> None:
        from modex_graph.persistence.deliver_store import _DELIVER_TABLE

        conn = sqlite3.connect(":memory:")
        store = SqliteDeliverStore(conn)
        columns = {
            row[1] for row in store._conn.execute(f"PRAGMA table_info({_DELIVER_TABLE})").fetchall()
        }
        assert "source_node_id" in columns
        assert "source_invocation_id" in columns
        assert "consumed_by_invocation_id" in columns
        conn.close()

    def test_graph_payload_sqlite_round_trip(self) -> None:
        from modex_graph import GraphPayload

        conn = sqlite3.connect(":memory:")
        store = SqliteDeliverStore(conn)
        payload = GraphPayload(content="start-to-end payload")
        store.accumulate(
            graph_instance_id=_GRAPH_INSTANCE_ID,
            node_id="node-a-001",
            source_node_id="node-source-001",
            source_invocation_id=0,
            content=payload,
        )
        result = store.query_consumable(_GRAPH_INSTANCE_ID, "node-a-001")
        assert len(result) == 1
        assert isinstance(result[0].content, GraphPayload)
        assert result[0].content.content == "start-to-end payload"
        conn.close()

    def test_old_schema_rebuilt_on_init(self) -> None:
        from modex_graph.persistence.deliver_store import _DELIVER_TABLE

        conn = sqlite3.connect(":memory:")
        # Create old-schema table without node_id column
        conn.execute(
            f"CREATE TABLE {_DELIVER_TABLE} ("
            "deliver_id INTEGER PRIMARY KEY, "
            "graph_instance_id INTEGER NOT NULL, "
            "content_json TEXT NOT NULL, "
            "status TEXT NOT NULL DEFAULT 'pending', "
            "created_at INTEGER NOT NULL, "
            "updated_at INTEGER NOT NULL)"
        )
        SqliteDeliverStore(conn)
        columns = {
            row[1] for row in conn.execute(f"PRAGMA table_info({_DELIVER_TABLE})").fetchall()
        }
        assert "node_id" in columns
        assert "next_node_id" in columns
        assert "source_node_id" in columns
        conn.close()


# ── Helper for sqlite integrity error ─────────────────────────────────────


def sqlite3_integrity_error() -> type[Exception]:
    import sqlite3

    return sqlite3.IntegrityError


# ── InMemoryDeliverStore specifics ────────────────────────────────────────


class TestInMemoryDeliverStoreSpecifics:
    def test_accumulate_sets_new_fields(self) -> None:
        store = InMemoryDeliverStore()
        deliver_id = store.accumulate(
            graph_instance_id=_GRAPH_INSTANCE_ID,
            node_id="node-worker-001",
            source_node_id="node-producer-002",
            source_invocation_id=42,
            content="payload",
        )
        records = store._records[_GRAPH_INSTANCE_ID]
        assert len(records) == 1
        r = records[0]
        assert r.deliver_id == deliver_id
        assert r.node_id == "node-worker-001"
        assert r.source_node_id == "node-producer-002"
        assert r.source_invocation_id == 42
        assert r.consumed_by_invocation_id is None
        assert r.status == DeliverConsumptionStatus.PENDING
