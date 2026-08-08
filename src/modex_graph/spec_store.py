"""In-memory and SQLite persistence adapters for declarative graph specs."""

from __future__ import annotations

import sqlite3
from abc import ABC, abstractmethod

from .id_generator import default_id_generator
from .persistence._time import now_ms
from .spec import GraphSpec
from .spec_record import GraphSpecRecord

# ── Table / column name constants ─────────────────────────────────────────
# The DDL/DML statements below are assembled from these constants; all data
# values go through `?` parameter placeholders.

_SPEC_TABLE = "graph_specs"
_COL_SPEC_ID = "spec_id"
_COL_NAME = "name"
_COL_VERSION = "version"
_COL_SPEC_JSON = "spec_json"
_COL_CREATED_AT = "created_at"
_COL_UPDATED_AT = "updated_at"


class GraphSpecStore(ABC):
    """Synchronous persistence contract for graph specs and metadata records."""

    @abstractmethod
    def save(self, spec: GraphSpec, spec_id: int | None = None) -> int:
        """Insert a `GraphSpec` or update the matching `(name, version)` record.

        Args:
            spec: The `GraphSpec` to persist.
            spec_id: Optional Snowflake ID for a new record. If `None`, one is
                generated via `default_id_generator()`.

        Returns:
            The existing or newly inserted `spec_id`.
        """
        ...

    @abstractmethod
    def load_by_id(self, spec_id: int) -> GraphSpec | None:
        """Load a `GraphSpec` by its `spec_id`.

        Args:
            spec_id: The Snowflake ID to look up.

        Returns:
            The `GraphSpec`, or `None` if no spec with this ID exists.
        """
        ...

    @abstractmethod
    def load_by_name(self, name: str, version: str = "1.0") -> GraphSpec | None:
        """Load a `GraphSpec` by `(name, version)`.

        Args:
            name: The spec name.
            version: The spec version (defaults to `"1.0"`).

        Returns:
            The `GraphSpec`, or `None` if no spec matches.
        """
        ...

    @abstractmethod
    def list_all(self) -> list[GraphSpec]:
        """List all persisted `GraphSpec`s.

        Returns:
            A list of all specs. Order is implementation-defined.
        """
        ...

    @abstractmethod
    def list_records(self) -> list[GraphSpecRecord]:
        """List persisted graph specification metadata without spec JSON."""
        ...

    @abstractmethod
    def get_by_id(self, spec_id: int) -> GraphSpecRecord | None:
        """Return metadata for a persisted graph specification ID."""
        ...

    @abstractmethod
    def delete(self, spec_id: int) -> None:
        """Delete a `GraphSpec` by `spec_id`.

        Args:
            spec_id: The Snowflake ID of the spec to delete.
        """
        ...


class InMemoryGraphSpecStore(GraphSpecStore):
    """Dictionary-backed graph specification store."""

    def __init__(self) -> None:
        self._specs: dict[int, GraphSpec] = {}
        self._by_name_version: dict[tuple[str, str], int] = {}
        self._created_at: dict[int, int] = {}

    def save(self, spec: GraphSpec, spec_id: int | None = None) -> int:
        key = (spec.name, spec.version)
        existing_id = self._by_name_version.get(key)
        if existing_id is not None:
            self._specs[existing_id] = spec
            return existing_id
        if spec_id is None:
            spec_id = default_id_generator().generate()
        self._specs[spec_id] = spec
        self._by_name_version[key] = spec_id
        self._created_at[spec_id] = now_ms()
        return spec_id

    def load_by_id(self, spec_id: int) -> GraphSpec | None:
        return self._specs.get(spec_id)

    def load_by_name(self, name: str, version: str = "1.0") -> GraphSpec | None:
        spec_id = self._by_name_version.get((name, version))
        if spec_id is None:
            return None
        return self._specs.get(spec_id)

    def list_all(self) -> list[GraphSpec]:
        return list(self._specs.values())

    def list_records(self) -> list[GraphSpecRecord]:
        return [
            GraphSpecRecord(
                spec_id=spec_id,
                name=spec.name,
                version=spec.version,
                created_at=self._created_at[spec_id],
            )
            for spec_id, spec in sorted(self._specs.items())
        ]

    def get_by_id(self, spec_id: int) -> GraphSpecRecord | None:
        spec = self._specs.get(spec_id)
        if spec is None:
            return None
        return GraphSpecRecord(
            spec_id=spec_id,
            name=spec.name,
            version=spec.version,
            created_at=self._created_at[spec_id],
        )

    def delete(self, spec_id: int) -> None:
        spec = self._specs.pop(spec_id, None)
        if spec is not None:
            self._by_name_version.pop((spec.name, spec.version), None)
            self._created_at.pop(spec_id)


class SqliteGraphSpecStore(GraphSpecStore):
    """SQLite graph specification store over a caller-owned connection."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._conn = connection
        self._init_schema()

    def _init_schema(self) -> None:
        """Create the `graph_specs` table + index if they don't exist.

        The DDL matches `001_initial.sql` table 16. The `json_valid` CHECK
        is omitted (same convention as `SqliteDeliverStore`); `UNIQUE
        (name, version)` is included for parity.
        """
        conn = self._conn
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {_SPEC_TABLE} ("
            f"{_COL_SPEC_ID} INTEGER PRIMARY KEY, "
            f"{_COL_NAME} TEXT NOT NULL, "
            f"{_COL_VERSION} TEXT NOT NULL DEFAULT '1.0', "
            f"{_COL_SPEC_JSON} TEXT NOT NULL, "
            f"{_COL_CREATED_AT} INTEGER NOT NULL, "
            f"{_COL_UPDATED_AT} INTEGER NOT NULL, "
            f"UNIQUE ({_COL_NAME}, {_COL_VERSION})"
            f")"
        )
        # Index matching the migration (idx_graph_specs_name).
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{_SPEC_TABLE}_name ON {_SPEC_TABLE} ({_COL_NAME})"
        )
        conn.commit()

    def save(self, spec: GraphSpec, spec_id: int | None = None) -> int:
        if spec_id is None:
            spec_id = default_id_generator().generate()
        ts = now_ms()
        spec_json = spec.model_dump_json()
        row = self._conn.execute(
            f"INSERT INTO {_SPEC_TABLE} "
            f"({_COL_SPEC_ID}, {_COL_NAME}, {_COL_VERSION}, "
            f"{_COL_SPEC_JSON}, {_COL_CREATED_AT}, {_COL_UPDATED_AT}) "
            f"VALUES (?, ?, ?, ?, ?, ?) "
            f"ON CONFLICT ({_COL_NAME}, {_COL_VERSION}) DO UPDATE SET "
            f"{_COL_SPEC_JSON} = excluded.{_COL_SPEC_JSON}, "
            f"{_COL_UPDATED_AT} = excluded.{_COL_UPDATED_AT} "
            f"RETURNING {_COL_SPEC_ID}",
            (spec_id, spec.name, spec.version, spec_json, ts, ts),
        ).fetchone()
        self._conn.commit()
        assert row is not None
        return int(row[0])

    def load_by_id(self, spec_id: int) -> GraphSpec | None:
        row = self._conn.execute(
            f"SELECT {_COL_SPEC_JSON} FROM {_SPEC_TABLE} WHERE {_COL_SPEC_ID} = ?",
            (spec_id,),
        ).fetchone()
        if row is None:
            return None
        return GraphSpec.model_validate_json(row[0])

    def load_by_name(self, name: str, version: str = "1.0") -> GraphSpec | None:
        row = self._conn.execute(
            f"SELECT {_COL_SPEC_JSON} FROM {_SPEC_TABLE} "
            f"WHERE {_COL_NAME} = ? AND {_COL_VERSION} = ?",
            (name, version),
        ).fetchone()
        if row is None:
            return None
        return GraphSpec.model_validate_json(row[0])

    def list_all(self) -> list[GraphSpec]:
        rows = self._conn.execute(
            f"SELECT {_COL_SPEC_JSON} FROM {_SPEC_TABLE} ORDER BY {_COL_SPEC_ID}"
        ).fetchall()
        return [GraphSpec.model_validate_json(r[0]) for r in rows]

    def list_records(self) -> list[GraphSpecRecord]:
        rows = self._conn.execute(
            f"SELECT {_COL_SPEC_ID}, {_COL_NAME}, {_COL_VERSION}, {_COL_CREATED_AT} "
            f"FROM {_SPEC_TABLE} ORDER BY {_COL_SPEC_ID}"
        ).fetchall()
        return [
            GraphSpecRecord(
                spec_id=row[0],
                name=row[1],
                version=row[2],
                created_at=row[3],
            )
            for row in rows
        ]

    def get_by_id(self, spec_id: int) -> GraphSpecRecord | None:
        row = self._conn.execute(
            f"SELECT {_COL_SPEC_ID}, {_COL_NAME}, {_COL_VERSION}, {_COL_CREATED_AT} "
            f"FROM {_SPEC_TABLE} WHERE {_COL_SPEC_ID} = ?",
            (spec_id,),
        ).fetchone()
        if row is None:
            return None
        return GraphSpecRecord(
            spec_id=row[0],
            name=row[1],
            version=row[2],
            created_at=row[3],
        )

    def delete(self, spec_id: int) -> None:
        self._conn.execute(
            f"DELETE FROM {_SPEC_TABLE} WHERE {_COL_SPEC_ID} = ?",
            (spec_id,),
        )
        self._conn.commit()


__all__ = [
    "GraphSpecStore",
    "InMemoryGraphSpecStore",
    "SqliteGraphSpecStore",
]
