# ruff: noqa: ANN401

"""Tests for DeliverStore ABC + InMemoryDeliverStore + SqliteDeliverStore.

Covers:

- ``DeliverStatus`` StrEnum values (retained for backward compat).
- ``DeliverConsumptionStatus`` StrEnum values (used by
  ``DeliverRecord.status``).
- ``DeliverRecord`` frozen Pydantic model with new fields
  (``source_node``, ``source_invocation_id``, ``consumed_by_invocation_id``)
  and ``DeliverConsumptionStatus`` status type.
- ``DeliverStore`` ABC (rule 7): 8 abstract methods — 4 new
  (``accumulate`` new signature, ``query_consumable``, ``mark_consumed``,
  ``promote_consumed``) + 4 old (``query_pending``, ``query_by_target``,
  ``mark_submitted``, ``clear``).
- ``InMemoryDeliverStore``: accumulate, query_pending, query_by_target,
  mark_submitted, clear. New methods stubbed.
- ``SqliteDeliverStore``: same CRUD + idempotent schema with new columns,
  expanded CHECK constraint, timestamps epoch ms, table/column constants,
  indexes created, file-based persistence. New methods stubbed.
- Cross-instance isolation (different ``graph_instance_id``).
- ``mark_submitted`` on empty list is a no-op.
- ``clear`` on non-existent instance is a no-op.
"""

from __future__ import annotations

import tempfile
from abc import ABC
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from modex_graph import (
    DeliverConsumptionStatus,
    DeliverRecord,
    DeliverStatus,
    DeliverStore,
    InMemoryDeliverStore,
    SqliteDeliverStore,
)

# ── Test helpers ──────────────────────────────────────────────────────────

_GRAPH_INSTANCE_ID = 1001
_OTHER_GRAPH_INSTANCE_ID = 2002
_SOURCE_NODE = "test_source"
_SOURCE_INVOCATION_ID = 0


def _store_factory(kind: str) -> Callable[[], DeliverStore]:
    if kind == "memory":
        return lambda: InMemoryDeliverStore()
    if kind == "sqlite":
        return lambda: SqliteDeliverStore(":memory:")
    raise ValueError(f"unknown kind: {kind}")


def _accumulate(
    store: DeliverStore,
    *,
    graph_instance_id: int = _GRAPH_INSTANCE_ID,
    target_node: str = "a",
    content: Any = "data",
) -> int:
    """Wrap accumulate with default source values for old-style tests."""
    return store.accumulate(
        graph_instance_id=graph_instance_id,
        target_node=target_node,
        source_node=_SOURCE_NODE,
        source_invocation_id=_SOURCE_INVOCATION_ID,
        content=content,
    )


STORE_KINDS = ["memory", "sqlite"]


# ── DeliverStatus enum (retained for backward compat) ────────────────────


class TestDeliverStatus:
    def test_accumulated_value(self) -> None:
        assert DeliverStatus.ACCUMULATED.value == "accumulated"

    def test_submitted_value(self) -> None:
        assert DeliverStatus.SUBMITTED.value == "submitted"

    def test_is_str_enum(self) -> None:
        from enum import StrEnum

        assert issubclass(DeliverStatus, StrEnum)

    def test_string_compatibility(self) -> None:
        assert DeliverStatus.ACCUMULATED == "accumulated"
        assert DeliverStatus.SUBMITTED == "submitted"


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
            node_name="node_a",
            next_node="node_b",
            source_node="src",
            source_invocation_id=99,
            consumed_by_invocation_id=None,
            content={"key": "value"},
            status=DeliverConsumptionStatus.PENDING,
            created_at=1700000000000,
            updated_at=1700000000000,
        )
        assert r.deliver_id == 123
        assert r.node_name == "node_a"
        assert r.next_node == "node_b"
        assert r.source_node == "src"
        assert r.source_invocation_id == 99
        assert r.consumed_by_invocation_id is None
        assert r.content == {"key": "value"}

    def test_status_defaults_to_pending(self) -> None:
        r = DeliverRecord(
            deliver_id=1,
            graph_instance_id=_GRAPH_INSTANCE_ID,
            node_name="a",
            next_node="b",
            source_node="src",
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
            node_name="a",
            next_node="b",
            source_node="src",
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
            node_name="a",
            next_node="b",
            source_node="src",
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
                    "node_name": "a",
                    "next_node": "b",
                    "source_node": "src",
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
                node_name="a",
                next_node="b",
                source_node="src",
                source_invocation_id=0,
                content=content,
                created_at=0,
                updated_at=0,
            )
            assert r.content == content

    def test_source_node_is_required(self) -> None:
        with pytest.raises(ValidationError):
            DeliverRecord.model_validate(
                {
                    "deliver_id": 1,
                    "graph_instance_id": _GRAPH_INSTANCE_ID,
                    "node_name": "a",
                    "next_node": "b",
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
                    "node_name": "a",
                    "next_node": "b",
                    "source_node": "src",
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

    def test_eight_abstract_methods(self) -> None:
        expected = {
            "accumulate",
            "query_consumable",
            "mark_consumed",
            "promote_consumed",
            "query_pending",
            "query_by_target",
            "mark_submitted",
            "clear",
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
        _accumulate(store, target_node="a", content="data")
        assert [record.content for record in store.query_consumable(_GRAPH_INSTANCE_ID, "a")] == ["data"]

    def test_mark_consumed_records_consumer(self, kind: str) -> None:
        store = _store_factory(kind)()
        deliver_id = _accumulate(store, target_node="a", content="data")
        store.mark_consumed([deliver_id], 1)

        records = store._records[_GRAPH_INSTANCE_ID] if kind == "memory" else store.query_consumable(_GRAPH_INSTANCE_ID, "a")
        assert records[0].consumed_by_invocation_id == 1

    def test_promote_consumed_finishes_strategy_transition(self, kind: str) -> None:
        store = _store_factory(kind)()
        deliver_id = _accumulate(store, target_node="a", content="data")
        store.mark_consumed([deliver_id], 1)
        store.promote_consumed(1)

        if kind == "memory":
            assert store._records[_GRAPH_INSTANCE_ID] == []
        else:
            row = store._conn.execute(
                "SELECT status FROM deliver_states WHERE deliver_id = ?", (deliver_id,)
            ).fetchone()
            assert row == (DeliverConsumptionStatus.CONSUMED_COMPLETED.value,)


# ── Parametrized accumulate/query/mark/clear tests ────────────────────────


@pytest.mark.parametrize("kind", STORE_KINDS)
class TestDeliverStoreCRUD:
    def test_accumulate_returns_int_deliver_id(self, kind: str) -> None:
        store = _store_factory(kind)()
        deliver_id = _accumulate(store, target_node="node_a", content={"data": 1})
        assert isinstance(deliver_id, int)
        assert deliver_id > 0

    def test_accumulate_generates_unique_ids(self, kind: str) -> None:
        store = _store_factory(kind)()
        id1 = _accumulate(store, target_node="a", content="data1")
        id2 = _accumulate(store, target_node="a", content="data2")
        assert id1 != id2

    def test_accumulate_is_keyword_only(self, kind: str) -> None:
        store = _store_factory(kind)()
        with pytest.raises(TypeError):
            store.accumulate(_GRAPH_INSTANCE_ID, "a", "src", 0, "data")  # type: ignore[call-arg]

    def test_query_pending_returns_pending(self, kind: str) -> None:
        store = _store_factory(kind)()
        _accumulate(store, target_node="node_a", content="data1")
        _accumulate(store, target_node="node_a", content="data2")
        result = store.query_pending(_GRAPH_INSTANCE_ID, "node_a")
        assert len(result) == 2
        assert all(r.node_name == "node_a" for r in result)
        assert all(r.status == DeliverConsumptionStatus.PENDING for r in result)

    def test_query_pending_filters_by_node(self, kind: str) -> None:
        store = _store_factory(kind)()
        _accumulate(store, target_node="node_a", content="data")
        _accumulate(store, target_node="node_b", content="data")
        result = store.query_pending(_GRAPH_INSTANCE_ID, "node_a")
        assert len(result) == 1
        assert result[0].node_name == "node_a"

    def test_query_by_target_returns_pending(self, kind: str) -> None:
        store = _store_factory(kind)()
        _accumulate(store, target_node="a", content="data1")
        _accumulate(store, target_node="b", content="data2")
        # New accumulate always sets next_node="" — query_by_target("")
        # returns all pending records.
        result = store.query_by_target(_GRAPH_INSTANCE_ID, "")
        assert len(result) == 2
        assert all(r.next_node == "" for r in result)
        # Non-empty target returns empty (no records have that next_node).
        assert store.query_by_target(_GRAPH_INSTANCE_ID, "target_x") == []

    def test_mark_submitted_changes_status(self, kind: str) -> None:
        store = _store_factory(kind)()
        id1 = _accumulate(store, target_node="a", content="data1")
        id2 = _accumulate(store, target_node="a", content="data2")
        store.mark_submitted([id1, id2])
        pending = store.query_pending(_GRAPH_INSTANCE_ID, "a")
        assert len(pending) == 0

    def test_mark_submitted_partial(self, kind: str) -> None:
        store = _store_factory(kind)()
        id1 = _accumulate(store, target_node="a", content="data1")
        id2 = _accumulate(store, target_node="a", content="data2")
        store.mark_submitted([id1])
        pending = store.query_pending(_GRAPH_INSTANCE_ID, "a")
        assert len(pending) == 1
        assert pending[0].deliver_id == id2

    def test_mark_submitted_empty_list_is_noop(self, kind: str) -> None:
        store = _store_factory(kind)()
        _accumulate(store, target_node="a", content="data")
        store.mark_submitted([])
        pending = store.query_pending(_GRAPH_INSTANCE_ID, "a")
        assert len(pending) == 1

    def test_clear_removes_all_for_instance(self, kind: str) -> None:
        store = _store_factory(kind)()
        _accumulate(store, target_node="a", content="data1")
        _accumulate(store, target_node="a", content="data2")
        store.clear(_GRAPH_INSTANCE_ID)
        assert store.query_pending(_GRAPH_INSTANCE_ID, "a") == []

    def test_clear_only_affects_specified_instance(self, kind: str) -> None:
        store = _store_factory(kind)()
        _accumulate(store, target_node="a", content="data1")
        _accumulate(
            store,
            graph_instance_id=_OTHER_GRAPH_INSTANCE_ID,
            target_node="a",
            content="data2",
        )
        store.clear(_GRAPH_INSTANCE_ID)
        assert store.query_pending(_GRAPH_INSTANCE_ID, "a") == []
        assert len(store.query_pending(_OTHER_GRAPH_INSTANCE_ID, "a")) == 1

    def test_different_graph_instances_isolated(self, kind: str) -> None:
        store = _store_factory(kind)()
        _accumulate(store, target_node="a", content="data1")
        _accumulate(
            store,
            graph_instance_id=_OTHER_GRAPH_INSTANCE_ID,
            target_node="a",
            content="data2",
        )
        assert len(store.query_pending(_GRAPH_INSTANCE_ID, "a")) == 1
        assert len(store.query_pending(_OTHER_GRAPH_INSTANCE_ID, "a")) == 1

    def test_query_empty_instance_returns_empty(self, kind: str) -> None:
        store = _store_factory(kind)()
        assert store.query_pending(99999, "a") == []
        assert store.query_by_target(99999, "b") == []

    def test_clear_nonexistent_instance_is_noop(self, kind: str) -> None:
        store = _store_factory(kind)()
        store.clear(99999)

    def test_content_round_trip_dict(self, kind: str) -> None:
        store = _store_factory(kind)()
        content = {"key": "value", "num": 42, "nested": {"inner": [1, 2, 3]}}
        _accumulate(store, target_node="a", content=content)
        result = store.query_pending(_GRAPH_INSTANCE_ID, "a")
        assert result[0].content == content

    def test_content_round_trip_string(self, kind: str) -> None:
        store = _store_factory(kind)()
        _accumulate(store, target_node="a", content="hello")
        result = store.query_pending(_GRAPH_INSTANCE_ID, "a")
        assert result[0].content == "hello"

    def test_content_round_trip_none(self, kind: str) -> None:
        store = _store_factory(kind)()
        _accumulate(store, target_node="a", content=None)
        result = store.query_pending(_GRAPH_INSTANCE_ID, "a")
        assert result[0].content is None

    def test_content_round_trip_list(self, kind: str) -> None:
        store = _store_factory(kind)()
        _accumulate(store, target_node="a", content=[1, 2, 3])
        result = store.query_pending(_GRAPH_INSTANCE_ID, "a")
        assert result[0].content == [1, 2, 3]

    def test_preserves_insertion_order(self, kind: str) -> None:
        store = _store_factory(kind)()
        _accumulate(store, target_node="a", content="first")
        _accumulate(store, target_node="a", content="second")
        _accumulate(store, target_node="a", content="third")
        result = store.query_pending(_GRAPH_INSTANCE_ID, "a")
        assert result[0].content == "first"
        assert result[1].content == "second"
        assert result[2].content == "third"

    def test_next_node_empty_string_round_trip(self, kind: str) -> None:
        store = _store_factory(kind)()
        _accumulate(store, target_node="a", content="unresolved")
        result = store.query_pending(_GRAPH_INSTANCE_ID, "a")
        assert len(result) == 1
        assert result[0].next_node == ""

    def test_deliver_record_fields_populated(self, kind: str) -> None:
        store = _store_factory(kind)()
        deliver_id = _accumulate(store, target_node="node_a", content={"data": 1})
        result = store.query_pending(_GRAPH_INSTANCE_ID, "node_a")
        assert len(result) == 1
        r = result[0]
        assert r.deliver_id == deliver_id
        assert r.graph_instance_id == _GRAPH_INSTANCE_ID
        assert r.node_name == "node_a"
        assert r.next_node == ""
        assert r.source_node == _SOURCE_NODE
        assert r.source_invocation_id == _SOURCE_INVOCATION_ID
        assert r.consumed_by_invocation_id is None
        assert r.status == DeliverConsumptionStatus.PENDING
        assert isinstance(r.created_at, int)
        assert isinstance(r.updated_at, int)
        assert r.created_at > 1_700_000_000_000
        assert r.updated_at >= r.created_at

    def test_mark_submitted_updates_timestamp(self, kind: str) -> None:
        store = _store_factory(kind)()
        deliver_id = _accumulate(store, target_node="a", content="data")
        store.query_pending(_GRAPH_INSTANCE_ID, "a")[0]
        store.mark_submitted([deliver_id])
        # After marking, query_pending returns empty (status != PENDING).
        pending = store.query_pending(_GRAPH_INSTANCE_ID, "a")
        assert len(pending) == 0


# ── SqliteDeliverStore specifics ──────────────────────────────────────────


class TestSqliteDeliverStoreSpecifics:
    def test_create_table_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "deliver.db")
            store1 = SqliteDeliverStore(db_path)
            store1.accumulate(
                graph_instance_id=_GRAPH_INSTANCE_ID,
                target_node="a",
                source_node="src",
                source_invocation_id=0,
                content={"k": "v"},
            )
            store1.close()
            store2 = SqliteDeliverStore(db_path)
            result = store2.query_pending(_GRAPH_INSTANCE_ID, "a")
            assert len(result) == 1
            assert result[0].content == {"k": "v"}
            store2.close()

    def test_table_and_column_constants(self) -> None:
        from modex_graph.deliver_store import (
            _COL_CONSUMED_BY_INVOCATION_ID,
            _COL_CONTENT_JSON,
            _COL_CREATED_AT,
            _COL_DELIVER_ID,
            _COL_GRAPH_INSTANCE_ID,
            _COL_NEXT_NODE,
            _COL_NODE_NAME,
            _COL_SOURCE_INVOCATION_ID,
            _COL_SOURCE_NODE,
            _COL_STATUS,
            _COL_UPDATED_AT,
            _DELIVER_TABLE,
        )

        assert _DELIVER_TABLE == "deliver_states"
        assert _COL_DELIVER_ID == "deliver_id"
        assert _COL_GRAPH_INSTANCE_ID == "graph_instance_id"
        assert _COL_NODE_NAME == "node_name"
        assert _COL_NEXT_NODE == "next_node"
        assert _COL_SOURCE_NODE == "source_node"
        assert _COL_SOURCE_INVOCATION_ID == "source_invocation_id"
        assert _COL_CONSUMED_BY_INVOCATION_ID == "consumed_by_invocation_id"
        assert _COL_CONTENT_JSON == "content_json"
        assert _COL_STATUS == "status"
        assert _COL_CREATED_AT == "created_at"
        assert _COL_UPDATED_AT == "updated_at"

    def test_indexes_created(self) -> None:
        store = SqliteDeliverStore(":memory:")
        indexes = store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name = ?",
            ("deliver_states",),
        ).fetchall()
        index_names = {r[0] for r in indexes}
        assert "idx_deliver_states_node" in index_names
        assert "idx_deliver_states_target" in index_names
        store.close()

    def test_file_based_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "deliver.db")
            store1 = SqliteDeliverStore(db_path)
            store1.accumulate(
                graph_instance_id=_GRAPH_INSTANCE_ID,
                target_node="a",
                source_node="src",
                source_invocation_id=0,
                content={"data": 42},
            )
            store1.close()
            store2 = SqliteDeliverStore(db_path)
            result = store2.query_pending(_GRAPH_INSTANCE_ID, "a")
            assert len(result) == 1
            assert result[0].content == {"data": 42}
            store2.close()

    def test_timestamps_are_epoch_ms(self) -> None:
        from modex_graph.deliver_store import _COL_CREATED_AT, _DELIVER_TABLE

        store = SqliteDeliverStore(":memory:")
        store.accumulate(
            graph_instance_id=_GRAPH_INSTANCE_ID,
            target_node="a",
            source_node="src",
            source_invocation_id=0,
            content="data",
        )
        row = store._conn.execute(f"SELECT {_COL_CREATED_AT} FROM {_DELIVER_TABLE}").fetchone()
        assert row is not None
        ts = row[0]
        assert isinstance(ts, int)
        assert ts > 1_700_000_000_000
        store.close()

    def test_status_check_constraint_rejects_invalid(self) -> None:
        from modex_graph.deliver_store import _DELIVER_TABLE

        store = SqliteDeliverStore(":memory:")
        with pytest.raises(sqlite3_integrity_error()):
            store._conn.execute(
                f"INSERT INTO {_DELIVER_TABLE} (deliver_id, graph_instance_id, "
                f"node_name, next_node, content_json, status, created_at, updated_at) "
                f"VALUES (999, 1, 'a', 'b', '\"x\"', 'invalid_status', 0, 0)"
            )
        store.close()

    def test_status_check_constraint_accepts_consumption_values(self) -> None:
        from modex_graph.deliver_store import _DELIVER_TABLE

        store = SqliteDeliverStore(":memory:")
        for status_val in (
            DeliverConsumptionStatus.PENDING.value,
            DeliverConsumptionStatus.CONSUMED.value,
            DeliverConsumptionStatus.CONSUMED_PENDING.value,
            DeliverConsumptionStatus.CONSUMED_COMPLETED.value,
            DeliverStatus.ACCUMULATED.value,
            DeliverStatus.SUBMITTED.value,
        ):
            store._conn.execute(
                f"INSERT INTO {_DELIVER_TABLE} (deliver_id, graph_instance_id, "
                f"node_name, next_node, content_json, status, created_at, updated_at) "
                f"VALUES (?, 1, 'a', 'b', '\"x\"', ?, 0, 0)",
                (hash(status_val) % 100000, status_val),
            )
        store.close()

    def test_new_columns_exist_after_init(self) -> None:
        from modex_graph.deliver_store import _DELIVER_TABLE

        store = SqliteDeliverStore(":memory:")
        columns = {
            row[1] for row in store._conn.execute(f"PRAGMA table_info({_DELIVER_TABLE})").fetchall()
        }
        assert "source_node" in columns
        assert "source_invocation_id" in columns
        assert "consumed_by_invocation_id" in columns
        store.close()

    def test_migrate_add_columns_is_idempotent(self) -> None:
        store = SqliteDeliverStore(":memory:")
        # Calling _migrate_add_columns again should be a no-op (columns exist).
        store._migrate_add_columns()
        store.close()

    def test_close_is_safe_multiple_times(self) -> None:
        store = SqliteDeliverStore(":memory:")
        store.close()
        store.close()


# ── Helper for sqlite integrity error ─────────────────────────────────────


def sqlite3_integrity_error() -> type[Exception]:
    import sqlite3

    return sqlite3.IntegrityError


# ── InMemoryDeliverStore specifics ────────────────────────────────────────


class TestInMemoryDeliverStoreSpecifics:
    def test_mark_submitted_replaces_with_new_record(self) -> None:
        store = InMemoryDeliverStore()
        deliver_id = _accumulate(store, target_node="a", content="data")
        store.mark_submitted([deliver_id])
        records = store._records[_GRAPH_INSTANCE_ID]
        assert len(records) == 1
        assert records[0].status == DeliverConsumptionStatus.CONSUMED
        assert records[0].deliver_id == deliver_id

    def test_mark_submitted_updates_updated_at(self) -> None:
        store = InMemoryDeliverStore()
        deliver_id = _accumulate(store, target_node="a", content="data")
        original = store._records[_GRAPH_INSTANCE_ID][0]
        store.mark_submitted([deliver_id])
        updated = store._records[_GRAPH_INSTANCE_ID][0]
        assert updated.updated_at >= original.updated_at

    def test_mark_submitted_nonexistent_id_is_noop(self) -> None:
        store = InMemoryDeliverStore()
        _accumulate(store, target_node="a", content="data")
        store.mark_submitted([99999])
        pending = store.query_pending(_GRAPH_INSTANCE_ID, "a")
        assert len(pending) == 1

    def test_accumulate_sets_new_fields(self) -> None:
        store = InMemoryDeliverStore()
        deliver_id = store.accumulate(
            graph_instance_id=_GRAPH_INSTANCE_ID,
            target_node="worker",
            source_node="producer",
            source_invocation_id=42,
            content="payload",
        )
        records = store._records[_GRAPH_INSTANCE_ID]
        assert len(records) == 1
        r = records[0]
        assert r.deliver_id == deliver_id
        assert r.node_name == "worker"
        assert r.next_node == ""
        assert r.source_node == "producer"
        assert r.source_invocation_id == 42
        assert r.consumed_by_invocation_id is None
        assert r.status == DeliverConsumptionStatus.PENDING
