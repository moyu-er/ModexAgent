"""Tests for GraphIORecordStore ABC + NullGraphIORecordStore +
InMemoryGraphIORecordStore + SqliteGraphIORecordStore.

Covers:

- `GraphIORecordStore` ABC (rule 7: ABC, not Protocol): 7 abstract methods.
- `GraphIORecord` frozen Pydantic value object (rule 12).
- `NullGraphIORecordStore`: no-op; `get` returns None.
- `InMemoryGraphIORecordStore`: save (upsert), get, get_by_instance,
  list_by_instance, list_by_spec, update_output, delete.
- `SqliteGraphIORecordStore`: same CRUD + idempotent schema, timestamps
  epoch ms, table/column constants, indexes created, file-based
  persistence, JSON round-trip for user_input and output, UPSERT via ON
  CONFLICT.
- Cross-record isolation.
- `update_output` on non-existent ID is a no-op.
- `delete` on non-existent ID is a no-op.
- Multiple records for same spec.
"""

from __future__ import annotations

import sqlite3
import tempfile
from abc import ABC
from collections.abc import Callable
from pathlib import Path

import pytest
from pydantic import ValidationError

from modex_graph import (
    GraphIORecord,
    GraphIORecordStore,
    GraphPayload,
    InMemoryGraphIORecordStore,
    NullGraphIORecordStore,
    SqliteGraphIORecordStore,
    default_id_generator,
)

# -- Test helpers ----------------------------------------------------------

_SPEC_ID = 5001
_OTHER_SPEC_ID = 6002
_GRAPH_INSTANCE_ID = 1001
_OTHER_INSTANCE_ID = 2002


def _gen_record_id() -> int:
    return default_id_generator().generate()


def _make_record(
    record_id: int | None = None,
    graph_instance_id: int = _GRAPH_INSTANCE_ID,
    spec_id: int = _SPEC_ID,
    user_input: GraphPayload | None = None,
    output: list[GraphPayload] | None = None,
    created_at: int = 0,
) -> GraphIORecord:
    return GraphIORecord(
        record_id=record_id if record_id is not None else _gen_record_id(),
        graph_instance_id=graph_instance_id,
        spec_id=spec_id,
        user_input=user_input,
        output=output,
        created_at=created_at,
    )


def _store_factory(kind: str) -> Callable[[], GraphIORecordStore]:
    if kind == "null":
        return lambda: NullGraphIORecordStore()
    if kind == "memory":
        return lambda: InMemoryGraphIORecordStore()
    if kind == "sqlite":
        return lambda: SqliteGraphIORecordStore(sqlite3.connect(":memory:"))
    raise ValueError(f"unknown kind: {kind}")


STORE_KINDS = ["memory", "sqlite"]


# -- GraphIORecord model ---------------------------------------------------


class TestGraphIORecord:
    def test_is_frozen(self) -> None:
        record = _make_record()
        with pytest.raises(ValidationError):
            record.record_id = 999  # type: ignore[misc]

    def test_extra_fields_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            GraphIORecord(
                record_id=1,
                graph_instance_id=2,
                spec_id=3,
                created_at=0,
                unexpected="oops",  # type: ignore[call-arg]
            )

    def test_defaults_none(self) -> None:
        record = GraphIORecord(
            record_id=1,
            graph_instance_id=2,
            spec_id=3,
            created_at=0,
        )
        assert record.user_input is None
        assert record.output is None


# -- GraphIORecordStore ABC ------------------------------------------------


class TestGraphIORecordStoreABC:
    def test_is_abc(self) -> None:
        assert issubclass(GraphIORecordStore, ABC)

    def test_cannot_instantiate_directly(self) -> None:
        with pytest.raises(TypeError):
            GraphIORecordStore()  # type: ignore[abstract]

    def test_seven_abstract_methods(self) -> None:
        expected = {
            "save",
            "get",
            "get_by_instance",
            "list_by_instance",
            "list_by_spec",
            "update_output",
            "delete",
        }
        assert set(GraphIORecordStore.__abstractmethods__) == expected

    def test_in_memory_is_subclass(self) -> None:
        assert issubclass(InMemoryGraphIORecordStore, GraphIORecordStore)

    def test_sqlite_is_subclass(self) -> None:
        assert issubclass(SqliteGraphIORecordStore, GraphIORecordStore)

    def test_null_is_subclass(self) -> None:
        assert issubclass(NullGraphIORecordStore, GraphIORecordStore)

    def test_is_not_protocol(self) -> None:
        from typing import Protocol

        assert not issubclass(GraphIORecordStore, Protocol)

    def test_in_memory_no_abstract_methods(self) -> None:
        assert len(InMemoryGraphIORecordStore.__abstractmethods__) == 0

    def test_sqlite_no_abstract_methods(self) -> None:
        assert len(SqliteGraphIORecordStore.__abstractmethods__) == 0

    def test_null_no_abstract_methods(self) -> None:
        assert len(NullGraphIORecordStore.__abstractmethods__) == 0


# -- NullGraphIORecordStore ------------------------------------------------


class TestNullGraphIORecordStore:
    def test_get_returns_none(self) -> None:
        store = NullGraphIORecordStore()
        assert store.get(1) is None

    def test_get_by_instance_returns_none(self) -> None:
        store = NullGraphIORecordStore()
        assert store.get_by_instance(_GRAPH_INSTANCE_ID) is None

    def test_list_by_instance_returns_empty(self) -> None:
        store = NullGraphIORecordStore()
        assert store.list_by_instance(_GRAPH_INSTANCE_ID) == []

    def test_list_by_spec_returns_empty(self) -> None:
        store = NullGraphIORecordStore()
        assert store.list_by_spec(_SPEC_ID) == []

    def test_save_is_noop(self) -> None:
        store = NullGraphIORecordStore()
        store.save(_make_record())
        assert store.get(1) is None

    def test_update_output_is_noop(self) -> None:
        store = NullGraphIORecordStore()
        store.update_output(1, [GraphPayload(content="out")])

    def test_delete_is_noop(self) -> None:
        store = NullGraphIORecordStore()
        store.delete(1)


# -- Parametrized CRUD tests -----------------------------------------------


@pytest.mark.parametrize("kind", STORE_KINDS)
class TestGraphIORecordStoreCRUD:
    def test_save_and_get(self, kind: str) -> None:
        store = _store_factory(kind)()
        record = _make_record(user_input=GraphPayload(content="hello"))
        store.save(record)
        loaded = store.get(record.record_id)
        assert loaded is not None
        assert loaded.record_id == record.record_id
        assert loaded.graph_instance_id == _GRAPH_INSTANCE_ID
        assert loaded.spec_id == _SPEC_ID
        assert loaded.user_input == GraphPayload(content="hello")
        assert loaded.output is None

    def test_get_returns_none_for_missing(self, kind: str) -> None:
        store = _store_factory(kind)()
        assert store.get(99999) is None

    def test_save_is_upsert(self, kind: str) -> None:
        store = _store_factory(kind)()
        rid = _gen_record_id()
        store.save(_make_record(record_id=rid, user_input=GraphPayload(content="old")))
        store.save(
            _make_record(record_id=rid, user_input=GraphPayload(content="new"))
        )
        loaded = store.get(rid)
        assert loaded is not None
        assert loaded.user_input == GraphPayload(content="new")

    def test_get_by_instance(self, kind: str) -> None:
        store = _store_factory(kind)()
        record = _make_record(graph_instance_id=_GRAPH_INSTANCE_ID)
        store.save(record)
        loaded = store.get_by_instance(_GRAPH_INSTANCE_ID)
        assert loaded is not None
        assert loaded.record_id == record.record_id

    def test_get_by_instance_returns_none_for_missing(self, kind: str) -> None:
        store = _store_factory(kind)()
        assert store.get_by_instance(99999) is None

    def test_list_by_instance(self, kind: str) -> None:
        store = _store_factory(kind)()
        store.save(_make_record(graph_instance_id=_GRAPH_INSTANCE_ID))
        store.save(_make_record(graph_instance_id=_OTHER_INSTANCE_ID))
        store.save(_make_record(graph_instance_id=_GRAPH_INSTANCE_ID))
        result = store.list_by_instance(_GRAPH_INSTANCE_ID)
        assert len(result) == 2
        assert all(r.graph_instance_id == _GRAPH_INSTANCE_ID for r in result)

    def test_list_by_instance_empty(self, kind: str) -> None:
        store = _store_factory(kind)()
        assert store.list_by_instance(99999) == []

    def test_list_by_spec(self, kind: str) -> None:
        store = _store_factory(kind)()
        store.save(_make_record(spec_id=_SPEC_ID))
        store.save(_make_record(spec_id=_OTHER_SPEC_ID))
        store.save(_make_record(spec_id=_SPEC_ID))
        result = store.list_by_spec(_SPEC_ID)
        assert len(result) == 2
        assert all(r.spec_id == _SPEC_ID for r in result)

    def test_list_by_spec_empty(self, kind: str) -> None:
        store = _store_factory(kind)()
        assert store.list_by_spec(99999) == []

    def test_update_output(self, kind: str) -> None:
        store = _store_factory(kind)()
        record = _make_record()
        store.save(record)
        new_output = [GraphPayload(content="result1"), GraphPayload(content="result2")]
        store.update_output(record.record_id, new_output)
        loaded = store.get(record.record_id)
        assert loaded is not None
        assert loaded.output == new_output
        assert loaded.user_input is None

    def test_update_output_to_none(self, kind: str) -> None:
        store = _store_factory(kind)()
        record = _make_record(output=[GraphPayload(content="initial")])
        store.save(record)
        store.update_output(record.record_id, None)
        loaded = store.get(record.record_id)
        assert loaded is not None
        assert loaded.output is None

    def test_update_output_nonexistent_is_noop(self, kind: str) -> None:
        store = _store_factory(kind)()
        store.update_output(99999, [GraphPayload(content="x")])

    def test_delete_removes_record(self, kind: str) -> None:
        store = _store_factory(kind)()
        record = _make_record()
        store.save(record)
        store.delete(record.record_id)
        assert store.get(record.record_id) is None

    def test_delete_nonexistent_is_noop(self, kind: str) -> None:
        store = _store_factory(kind)()
        store.delete(99999)

    def test_different_records_isolated(self, kind: str) -> None:
        store = _store_factory(kind)()
        r1 = _make_record(
            graph_instance_id=1, user_input=GraphPayload(content="a")
        )
        r2 = _make_record(
            graph_instance_id=2, user_input=GraphPayload(content="b")
        )
        store.save(r1)
        store.save(r2)
        assert store.get(r1.record_id).user_input == GraphPayload(content="a")  # type: ignore[union-attr]
        assert store.get(r2.record_id).user_input == GraphPayload(content="b")  # type: ignore[union-attr]

    def test_multiple_records_same_spec(self, kind: str) -> None:
        store = _store_factory(kind)()
        store.save(_make_record(graph_instance_id=1, spec_id=_SPEC_ID))
        store.save(_make_record(graph_instance_id=2, spec_id=_SPEC_ID))
        store.save(_make_record(graph_instance_id=3, spec_id=_SPEC_ID))
        result = store.list_by_spec(_SPEC_ID)
        assert len(result) == 3
        instance_ids = {r.graph_instance_id for r in result}
        assert instance_ids == {1, 2, 3}

    def test_user_input_round_trip(self, kind: str) -> None:
        store = _store_factory(kind)()
        record = _make_record(
            user_input=GraphPayload(content="user query payload")
        )
        store.save(record)
        loaded = store.get(record.record_id)
        assert loaded is not None
        assert loaded.user_input == GraphPayload(content="user query payload")

    def test_output_round_trip(self, kind: str) -> None:
        store = _store_factory(kind)()
        output = [GraphPayload(content="out1"), GraphPayload(content="out2")]
        record = _make_record(output=output)
        store.save(record)
        loaded = store.get(record.record_id)
        assert loaded is not None
        assert loaded.output == output

    def test_none_user_input_and_output_round_trip(self, kind: str) -> None:
        store = _store_factory(kind)()
        record = _make_record(user_input=None, output=None)
        store.save(record)
        loaded = store.get(record.record_id)
        assert loaded is not None
        assert loaded.user_input is None
        assert loaded.output is None


# -- SqliteGraphIORecordStore specifics ------------------------------------


class TestSqliteGraphIORecordStoreSpecifics:
    def test_create_table_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "io_records.db")
            conn1 = sqlite3.connect(db_path)
            store1 = SqliteGraphIORecordStore(conn1)
            record = _make_record(user_input=GraphPayload(content="persist"))
            store1.save(record)
            conn1.close()
            conn2 = sqlite3.connect(db_path)
            store2 = SqliteGraphIORecordStore(conn2)
            loaded = store2.get(record.record_id)
            assert loaded is not None
            assert loaded.user_input == GraphPayload(content="persist")
            conn2.close()

    def test_table_and_column_constants(self) -> None:
        from modex_graph.persistence.graph_io_store import (
            _COL_CREATED_AT,
            _COL_GRAPH_INSTANCE_ID,
            _COL_OUTPUT_JSON,
            _COL_RECORD_ID,
            _COL_SPEC_ID,
            _COL_USER_INPUT_JSON,
            _IO_TABLE,
        )

        assert _IO_TABLE == "graph_io_records"
        assert _COL_RECORD_ID == "record_id"
        assert _COL_GRAPH_INSTANCE_ID == "graph_instance_id"
        assert _COL_SPEC_ID == "spec_id"
        assert _COL_USER_INPUT_JSON == "user_input_json"
        assert _COL_OUTPUT_JSON == "output_json"
        assert _COL_CREATED_AT == "created_at"

    def test_indexes_created(self) -> None:
        conn = sqlite3.connect(":memory:")
        store = SqliteGraphIORecordStore(conn)
        indexes = store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name = ?",
            ("graph_io_records",),
        ).fetchall()
        index_names = {r[0] for r in indexes}
        assert "idx_graph_io_records_instance" in index_names
        assert "idx_graph_io_records_spec" in index_names
        conn.close()

    def test_foreign_key_declared(self) -> None:
        conn = sqlite3.connect(":memory:")
        store = SqliteGraphIORecordStore(conn)
        fks = store._conn.execute(
            "PRAGMA foreign_key_list(graph_io_records)"
        ).fetchall()
        # PRAGMA foreign_key_list row: (id, seq, table, from, to, on_update, on_delete, match)
        fk_targets = {fk[2] for fk in fks}
        assert "graph_instances" in fk_targets, (
            f"Expected FK to graph_instances, got: {fks}"
        )
        conn.close()

    def test_file_based_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "io_records.db")
            conn1 = sqlite3.connect(db_path)
            store1 = SqliteGraphIORecordStore(conn1)
            record = _make_record(
                graph_instance_id=42,
                output=[GraphPayload(content="file-persist")],
            )
            store1.save(record)
            conn1.close()
            conn2 = sqlite3.connect(db_path)
            store2 = SqliteGraphIORecordStore(conn2)
            loaded = store2.get(record.record_id)
            assert loaded is not None
            assert loaded.output == [GraphPayload(content="file-persist")]
            conn2.close()

    def test_timestamps_are_epoch_ms(self) -> None:
        from modex_graph.persistence.graph_io_store import _COL_CREATED_AT, _IO_TABLE

        conn = sqlite3.connect(":memory:")
        store = SqliteGraphIORecordStore(conn)
        record = _make_record(created_at=1_700_000_000_000)
        store.save(record)
        row = store._conn.execute(
            f"SELECT {_COL_CREATED_AT} FROM {_IO_TABLE} WHERE record_id = ?",
            (record.record_id,),
        ).fetchone()
        assert row is not None
        assert row[0] == 1_700_000_000_000
        conn.close()

    def test_upsert_via_on_conflict(self) -> None:
        conn = sqlite3.connect(":memory:")
        store = SqliteGraphIORecordStore(conn)
        rid = _gen_record_id()
        store.save(_make_record(record_id=rid, user_input=GraphPayload(content="v1")))
        store.save(_make_record(record_id=rid, user_input=GraphPayload(content="v2")))
        store.save(_make_record(record_id=rid, user_input=GraphPayload(content="v3")))
        loaded = store.get(rid)
        assert loaded is not None
        assert loaded.user_input == GraphPayload(content="v3")
        rows = store._conn.execute(
            "SELECT COUNT(*) FROM graph_io_records WHERE record_id = ?",
            (rid,),
        ).fetchone()
        assert rows[0] == 1
        conn.close()

    def test_user_input_stored_as_null_when_none(self) -> None:
        from modex_graph.persistence.graph_io_store import (
            _COL_USER_INPUT_JSON,
            _IO_TABLE,
        )

        conn = sqlite3.connect(":memory:")
        store = SqliteGraphIORecordStore(conn)
        record = _make_record(user_input=None)
        store.save(record)
        row = store._conn.execute(
            f"SELECT {_COL_USER_INPUT_JSON} FROM {_IO_TABLE} WHERE record_id = ?",
            (record.record_id,),
        ).fetchone()
        assert row is not None
        assert row[0] is None  # SQL NULL, not the string "null"
        conn.close()

    def test_output_stored_as_null_when_none(self) -> None:
        from modex_graph.persistence.graph_io_store import _COL_OUTPUT_JSON, _IO_TABLE

        conn = sqlite3.connect(":memory:")
        store = SqliteGraphIORecordStore(conn)
        record = _make_record(output=None)
        store.save(record)
        row = store._conn.execute(
            f"SELECT {_COL_OUTPUT_JSON} FROM {_IO_TABLE} WHERE record_id = ?",
            (record.record_id,),
        ).fetchone()
        assert row is not None
        assert row[0] is None  # SQL NULL
        conn.close()

    def test_update_output_sets_json(self) -> None:
        from modex_graph.persistence.graph_io_store import _COL_OUTPUT_JSON, _IO_TABLE

        conn = sqlite3.connect(":memory:")
        store = SqliteGraphIORecordStore(conn)
        record = _make_record()
        store.save(record)
        store.update_output(record.record_id, [GraphPayload(content="updated")])
        row = store._conn.execute(
            f"SELECT {_COL_OUTPUT_JSON} FROM {_IO_TABLE} WHERE record_id = ?",
            (record.record_id,),
        ).fetchone()
        assert row is not None
        assert row[0] is not None
        conn.close()


# -- InMemoryGraphIORecordStore specifics ----------------------------------


class TestInMemoryGraphIORecordStoreSpecifics:
    def test_internal_dict_keyed_by_id(self) -> None:
        store = InMemoryGraphIORecordStore()
        record = _make_record()
        store.save(record)
        assert record.record_id in store._records

    def test_update_output_replaces_with_new_instance(self) -> None:
        store = InMemoryGraphIORecordStore()
        record = _make_record()
        store.save(record)
        original = store._records[record.record_id]
        store.update_output(record.record_id, [GraphPayload(content="new")])
        updated = store._records[record.record_id]
        assert updated.output == [GraphPayload(content="new")]
        assert original.output is None
        assert updated is not original

    def test_save_upsert_replaces(self) -> None:
        store = InMemoryGraphIORecordStore()
        rid = _gen_record_id()
        store.save(_make_record(record_id=rid, user_input=GraphPayload(content="old")))
        store.save(_make_record(record_id=rid, user_input=GraphPayload(content="new")))
        assert len(store._records) == 1
        assert store._records[rid].user_input == GraphPayload(content="new")
