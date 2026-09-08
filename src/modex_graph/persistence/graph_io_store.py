"""`GraphIORecordStore` — persistence for graph I/O records (input + output).

Provides:

- `GraphIORecord` — frozen Pydantic value object (rule 12) storing the
  input and output payloads for one graph instance execution.
- `GraphIORecordStore` ABC (rule 7: ABC, not Protocol) — the minimal
  interface for saving and querying `GraphIORecord` rows keyed by
  `record_id` (Snowflake, primary key).
- `NullGraphIORecordStore` — no-op; `get` returns None.
- `InMemoryGraphIORecordStore` — dict-backed default. In-process only.
- `SqliteGraphIORecordStore` — SQLite adapter. `CREATE TABLE IF NOT EXISTS
  graph_io_records` with field-by-field column mapping plus JSON columns
  for `user_input` and `output`.

The store persists the input/output payloads separately from
`GraphMetadata` (which holds identity/status). Status and `updated_at`
are NOT duplicated here — they come from the instance table via join.

Follows the same store pattern as `instance_store.py`: ABC + Null +
InMemory + SQLite, centralized table/column constants, `CREATE TABLE IF
NOT EXISTS`, `?` placeholders. The SQLite adapter takes a caller-owned
`sqlite3.Connection` and never closes it.
"""

from __future__ import annotations

import sqlite3
from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, ConfigDict, TypeAdapter

from ..integration import GraphPayload

# -- Table / column name constants -----------------------------------------
_IO_TABLE = "graph_io_records"
_COL_RECORD_ID = "record_id"
_COL_GRAPH_INSTANCE_ID = "graph_instance_id"
_COL_SPEC_ID = "spec_id"
_COL_VERSION = "version"
_COL_GRAPH_RUN_VERSION = "graph_run_version"
_COL_USER_INPUT_JSON = "user_input_json"
_COL_OUTPUT_JSON = "output_json"
_COL_CREATED_AT = "created_at"

_SELECT_COLS = (
    f"{_COL_RECORD_ID}, {_COL_GRAPH_INSTANCE_ID}, {_COL_SPEC_ID}, "
    f"{_COL_VERSION}, {_COL_USER_INPUT_JSON}, {_COL_OUTPUT_JSON}, {_COL_CREATED_AT}, {_COL_GRAPH_RUN_VERSION}"
)

# TypeAdapters for JSON serialization of nullable payload fields (same
# pattern as `_NODE_ID_MAP_ADAPTER` in instance_store.py).
_USER_INPUT_ADAPTER = TypeAdapter(GraphPayload | None)
_OUTPUT_ADAPTER = TypeAdapter(list[GraphPayload] | None)


class GraphIORecord(BaseModel):
    """One graph invocation's input + output record. Frozen value object (rule 12).

    Version-scoped per ADR-0040: each invocation of a graph instance gets
    its own IORecord, with ``version`` aligned to ``GraphMetadata.version``.

    Fields:

    - ``record_id: int`` -- Snowflake ID (primary key).
    - ``graph_instance_id: int`` -- FK -> ``graph_instances``.
    - ``spec_id: int`` -- FK -> ``graph_specs``.
    - ``version: int`` -- the graph instance version (invocation number)
      this record belongs to. Aligned with ``GraphMetadata.version``.
    - ``graph_run_version: int | None`` -- original graph version at fresh
      admission; equality identifies the same logical run across recovery.
      None retains the membership of legacy/unscoped records.
    - ``user_input: GraphPayload | None`` -- the user input payload
      (None if the graph had no explicit input).
    - ``output: list[GraphPayload] | None`` -- the output payloads
      collected during execution (None until the graph completes).
    - ``created_at: int`` -- epoch ms.

    Status and ``updated_at`` are intentionally NOT stored here -- they
    come from the ``graph_instances`` table via join.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    record_id: int
    graph_instance_id: int
    spec_id: int
    version: int
    user_input: GraphPayload | None = None
    output: list[GraphPayload] | None = None
    created_at: int
    # Original graph version at fresh admission; None for persisted legacy rows.
    graph_run_version: int | None = None


class GraphIORecordStore(ABC):
    """Persistence abstraction for `GraphIORecord` rows (rule 7: ABC).

    The store is keyed by `record_id` — a Snowflake ID (BIGINT) that is
    the primary key. Each graph invocation gets its own I/O record,
    version-scoped per ADR-0040. An instance may have multiple records
    (one per version). Use `get_latest_by_instance` for the active
    version or `list_by_instance` for all versions.

    All methods are synchronous and must be called from the event-loop
    thread only. The caller owns the ``sqlite3.Connection`` and manages
    its lifetime -- the store never closes it.

    Implementations:

    - `NullGraphIORecordStore` -- no-op; `get` returns None.
    - `InMemoryGraphIORecordStore` -- dict-backed, default.
    - `SqliteGraphIORecordStore` -- SQLite file or `:memory:`.
    """

    @abstractmethod
    def save(self, record: GraphIORecord) -> None:
        """Save (insert or update) a `GraphIORecord` row.

        If a row with this `record_id` exists, it is updated; otherwise a
        new row is inserted (UPSERT).

        Args:
            record: The `GraphIORecord` to persist. The `record_id`
                field must be set.
        """
        ...

    @abstractmethod
    def get(self, record_id: int) -> GraphIORecord | None:
        """Load a `GraphIORecord` by `record_id`.

        Args:
            record_id: The Snowflake ID to look up.

        Returns:
            The `GraphIORecord`, or `None` if not found.
        """
        ...

    @abstractmethod
    def get_latest_by_instance(self, graph_instance_id: int) -> GraphIORecord | None:
        """Load the latest (highest version) I/O record for a graph instance.

        Args:
            graph_instance_id: The graph instance ID.

        Returns:
            The ``GraphIORecord`` with the highest ``version``, or ``None``.
        """
        ...

    @abstractmethod
    def list_by_instance(self, graph_instance_id: int) -> list[GraphIORecord]:
        """List all I/O records for a given graph instance, ordered by version.

        Args:
            graph_instance_id: The graph instance ID.

        Returns:
            All ``GraphIORecord`` rows for this instance, ordered by ``version``.
        """
        ...

    @abstractmethod
    def list_by_spec(self, spec_id: int) -> list[GraphIORecord]:
        """List all I/O records for a given spec.

        Args:
            spec_id: The graph spec ID (FK -> graph_specs).

        Returns:
            All `GraphIORecord` rows for this spec, ordered by
            `(graph_instance_id, version)`.
        """
        ...

    @abstractmethod
    def update_output(self, record_id: int, output: list[GraphPayload] | None) -> None:
        """Update only the `output` field of a record.

        Args:
            record_id: The record to update.
            output: The new output payloads (or None to clear).
        """
        ...

    @abstractmethod
    def delete(self, record_id: int) -> None:
        """Delete a `GraphIORecord` by `record_id`.

        Args:
            record_id: The Snowflake ID of the record to delete.
        """
        ...


class NullGraphIORecordStore(GraphIORecordStore):
    """No-op `GraphIORecordStore` -- `get` returns None, writes are silent.

    The Null strategy: every method is a no-op and ``get`` returns
    ``None`` so callers see no persisted I/O records.
    """

    def save(self, record: GraphIORecord) -> None:
        pass

    def get(self, record_id: int) -> GraphIORecord | None:
        return None

    def get_latest_by_instance(self, graph_instance_id: int) -> GraphIORecord | None:
        return None

    def list_by_instance(self, graph_instance_id: int) -> list[GraphIORecord]:
        return []

    def list_by_spec(self, spec_id: int) -> list[GraphIORecord]:
        return []

    def update_output(self, record_id: int, output: list[GraphPayload] | None) -> None:
        pass

    def delete(self, record_id: int) -> None:
        pass


class InMemoryGraphIORecordStore(GraphIORecordStore):
    """Default in-memory `GraphIORecordStore` -- dict keyed by `record_id`.

    Suitable for single-process runs and tests. Not persistent across
    process restarts.
    """

    def __init__(self) -> None:
        self._records: dict[int, GraphIORecord] = {}

    def save(self, record: GraphIORecord) -> None:
        self._records[record.record_id] = record

    def get(self, record_id: int) -> GraphIORecord | None:
        return self._records.get(record_id)

    def get_latest_by_instance(self, graph_instance_id: int) -> GraphIORecord | None:
        records = [
            r for r in self._records.values()
            if r.graph_instance_id == graph_instance_id
        ]
        if not records:
            return None
        return max(records, key=lambda r: r.version)

    def list_by_instance(self, graph_instance_id: int) -> list[GraphIORecord]:
        records = [
            r for r in self._records.values()
            if r.graph_instance_id == graph_instance_id
        ]
        return sorted(records, key=lambda r: r.version)

    def list_by_spec(self, spec_id: int) -> list[GraphIORecord]:
        records = [r for r in self._records.values() if r.spec_id == spec_id]
        return sorted(records, key=lambda r: (r.graph_instance_id, r.version))

    def update_output(self, record_id: int, output: list[GraphPayload] | None) -> None:
        existing = self._records.get(record_id)
        if existing is None:
            return
        # Frozen model -- replace with a new instance with updated output.
        self._records[record_id] = existing.model_copy(update={"output": output})

    def delete(self, record_id: int) -> None:
        self._records.pop(record_id, None)


class SqliteGraphIORecordStore(GraphIORecordStore):
    """SQLite-backed `GraphIORecordStore` using stdlib `sqlite3`.

    Schema is created on construction via `CREATE TABLE IF NOT EXISTS`
    (lightweight migration). Missing nullable membership columns are added
    without rewriting existing records; repeated initialization is a no-op.

    Table and column names are module-level constants; all data values go
    through `?` parameter placeholders (no string interpolation, no SQL
    injection surface).

    The `user_input` and `output` fields are serialized to JSON text on
    write (via `TypeAdapter`) and deserialized on read. When the field is
    ``None``, the column stores SQL ``NULL`` (not the JSON string
    ``"null"``).

    Timestamps are epoch milliseconds (`now_ms()`), per ADR-0029.

    `save` uses SQLite UPSERT (`INSERT ... ON CONFLICT DO UPDATE`) so the
    same method handles both insert and update by `record_id` PK.
    `update_output` uses `UPDATE ... SET output_json = ? WHERE record_id
    = ?`.

    The store uses a single caller-owned ``sqlite3.Connection`` for its
    lifetime. The caller creates the connection (with ``check_same_thread``
    set as needed) and passes it to all stores sharing one workspace DB;
    the store never closes it.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._conn = connection
        self._init_schema()

    def _init_schema(self) -> None:
        """Create the `graph_io_records` table + indexes if they don't exist."""
        conn = self._conn
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {_IO_TABLE} ("
            f"{_COL_RECORD_ID} INTEGER PRIMARY KEY, "
            f"{_COL_GRAPH_INSTANCE_ID} INTEGER NOT NULL "
            f"REFERENCES graph_instances(graph_instance_id), "
            f"{_COL_SPEC_ID} INTEGER NOT NULL, "
            f"{_COL_VERSION} INTEGER NOT NULL DEFAULT 0, "
            f"{_COL_GRAPH_RUN_VERSION} INTEGER, "
            f"{_COL_USER_INPUT_JSON} TEXT, "
            f"{_COL_OUTPUT_JSON} TEXT, "
            f"{_COL_CREATED_AT} INTEGER NOT NULL"
            f")"
        )
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({_IO_TABLE})")}
        if _COL_GRAPH_RUN_VERSION not in columns:
            conn.execute(f"ALTER TABLE {_IO_TABLE} ADD COLUMN {_COL_GRAPH_RUN_VERSION} INTEGER")
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{_IO_TABLE}_instance "
            f"ON {_IO_TABLE} ({_COL_GRAPH_INSTANCE_ID}, {_COL_VERSION} DESC)"
        )
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{_IO_TABLE}_spec "
            f"ON {_IO_TABLE} ({_COL_SPEC_ID})"
        )
        conn.commit()

    def save(self, record: GraphIORecord) -> None:
        user_input_json = (
            _USER_INPUT_ADAPTER.dump_json(record.user_input).decode("utf-8")
            if record.user_input is not None
            else None
        )
        output_json = (
            _OUTPUT_ADAPTER.dump_json(record.output).decode("utf-8")
            if record.output is not None
            else None
        )
        self._conn.execute(
            f"INSERT INTO {_IO_TABLE} "
            f"({_COL_RECORD_ID}, {_COL_GRAPH_INSTANCE_ID}, {_COL_SPEC_ID}, "
            f"{_COL_VERSION}, {_COL_USER_INPUT_JSON}, {_COL_OUTPUT_JSON}, {_COL_CREATED_AT}, {_COL_GRAPH_RUN_VERSION}) "
            f"VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            f"ON CONFLICT({_COL_RECORD_ID}) DO UPDATE SET "
            f"{_COL_GRAPH_INSTANCE_ID} = excluded.{_COL_GRAPH_INSTANCE_ID}, "
            f"{_COL_SPEC_ID} = excluded.{_COL_SPEC_ID}, "
            f"{_COL_VERSION} = excluded.{_COL_VERSION}, "
            f"{_COL_GRAPH_RUN_VERSION} = excluded.{_COL_GRAPH_RUN_VERSION}, "
            f"{_COL_USER_INPUT_JSON} = excluded.{_COL_USER_INPUT_JSON}, "
            f"{_COL_OUTPUT_JSON} = excluded.{_COL_OUTPUT_JSON}, "
            f"{_COL_CREATED_AT} = excluded.{_COL_CREATED_AT}",
            (
                record.record_id,
                record.graph_instance_id,
                record.spec_id,
                record.version,
                user_input_json,
                output_json,
                record.created_at,
                record.graph_run_version,
            ),
        )
        self._conn.commit()

    def get(self, record_id: int) -> GraphIORecord | None:
        row = self._conn.execute(
            f"SELECT {_SELECT_COLS} FROM {_IO_TABLE} "
            f"WHERE {_COL_RECORD_ID} = ?",
            (record_id,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_record(row)

    def get_latest_by_instance(self, graph_instance_id: int) -> GraphIORecord | None:
        row = self._conn.execute(
            f"SELECT {_SELECT_COLS} FROM {_IO_TABLE} "
            f"WHERE {_COL_GRAPH_INSTANCE_ID} = ? "
            f"ORDER BY {_COL_VERSION} DESC "
            f"LIMIT 1",
            (graph_instance_id,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_record(row)

    def list_by_instance(self, graph_instance_id: int) -> list[GraphIORecord]:
        rows = self._conn.execute(
            f"SELECT {_SELECT_COLS} FROM {_IO_TABLE} "
            f"WHERE {_COL_GRAPH_INSTANCE_ID} = ? "
            f"ORDER BY {_COL_VERSION}",
            (graph_instance_id,),
        ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def list_by_spec(self, spec_id: int) -> list[GraphIORecord]:
        rows = self._conn.execute(
            f"SELECT {_SELECT_COLS} FROM {_IO_TABLE} "
            f"WHERE {_COL_SPEC_ID} = ? "
            f"ORDER BY {_COL_GRAPH_INSTANCE_ID}, {_COL_VERSION}",
            (spec_id,),
        ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def update_output(self, record_id: int, output: list[GraphPayload] | None) -> None:
        output_json = (
            _OUTPUT_ADAPTER.dump_json(output).decode("utf-8")
            if output is not None
            else None
        )
        self._conn.execute(
            f"UPDATE {_IO_TABLE} "
            f"SET {_COL_OUTPUT_JSON} = ? "
            f"WHERE {_COL_RECORD_ID} = ?",
            (output_json, record_id),
        )
        self._conn.commit()

    def delete(self, record_id: int) -> None:
        self._conn.execute(
            f"DELETE FROM {_IO_TABLE} WHERE {_COL_RECORD_ID} = ?",
            (record_id,),
        )
        self._conn.commit()

    @staticmethod
    def _row_to_record(row: tuple[Any, ...]) -> GraphIORecord:
        """Construct a `GraphIORecord` from a DB row."""
        (
            record_id,
            graph_instance_id,
            spec_id,
            version,
            user_input_json,
            output_json,
            created_at,
            graph_run_version,
        ) = row
        return GraphIORecord(
            record_id=record_id,
            graph_instance_id=graph_instance_id,
            spec_id=spec_id,
            version=version,
            user_input=(
                _USER_INPUT_ADAPTER.validate_json(user_input_json)
                if user_input_json is not None
                else None
            ),
            output=(
                _OUTPUT_ADAPTER.validate_json(output_json)
                if output_json is not None
                else None
            ),
            created_at=created_at,
            graph_run_version=graph_run_version,
        )


__all__ = [
    "GraphIORecord",
    "GraphIORecordStore",
    "InMemoryGraphIORecordStore",
    "NullGraphIORecordStore",
    "SqliteGraphIORecordStore",
]
