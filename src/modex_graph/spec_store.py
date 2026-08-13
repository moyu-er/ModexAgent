"""In-memory and SQLite persistence adapters for declarative graph specs.

Spec is immutable (ADR-0040 change 3): each save with changed content creates
a new row with a new Snowflake ``spec_id``. ``save`` always INSERTs;
``save_if_changed`` deduplicates by comparing ``spec_json`` against the latest
row for the same name. ``list_records`` returns only the latest ``spec_id``
per name (``MAX(spec_id) GROUP BY name`` — Snowflake IDs are time-ordered).
Historical specs are accessible only via ``get_by_id`` / ``load_by_id``.
"""

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
    """Synchronous persistence contract for immutable graph specs (ADR-0040)."""

    @abstractmethod
    def save(self, spec: GraphSpec, spec_id: int | None = None) -> int:
        """INSERT a new ``GraphSpec`` row with a new Snowflake ``spec_id``.

        Always creates a new row — never overwrites. Use ``save_if_changed``
        for content-deduplicated saves.

        Args:
            spec: The ``GraphSpec`` to persist.
            spec_id: Optional Snowflake ID for the new record. If ``None``,
                one is generated via ``default_id_generator()``.

        Returns:
            The newly inserted ``spec_id``.
        """
        ...

    @abstractmethod
    def save_if_changed(self, spec: GraphSpec, spec_id: int | None = None) -> int:
        """Content-deduplicated save (ADR-0040 change 3).

        Compares ``spec.model_dump_json()`` against the latest existing row
        for the same ``name`` (``MAX(spec_id) WHERE name = ?``). If identical,
        returns the existing ``spec_id`` (idempotent — no new row). If
        different or no prior row exists, INSERTs a new row and returns the
        new ``spec_id``.

        Args:
            spec: The ``GraphSpec`` to persist.
            spec_id: Optional Snowflake ID for a new record. If ``None``,
                one is generated via ``default_id_generator()``.

        Returns:
            The existing or newly inserted ``spec_id``.
        """
        ...

    @abstractmethod
    def load_by_id(self, spec_id: int) -> GraphSpec | None:
        """Load a ``GraphSpec`` by its ``spec_id``.

        Args:
            spec_id: The Snowflake ID to look up.

        Returns:
            The ``GraphSpec``, or ``None`` if no spec with this ID exists.
        """
        ...

    @abstractmethod
    def load_by_name(self, name: str, version: str = "1.0") -> GraphSpec | None:
        """Load the latest ``GraphSpec`` for a given ``name``.

        ``version`` is a display label (ADR-0040), not a lookup key — the
        latest row (``MAX(spec_id)``) for the name is returned regardless
        of the ``version`` argument.

        Args:
            name: The spec name.
            version: Ignored (kept for signature stability).

        Returns:
            The latest ``GraphSpec`` for the name, or ``None``.
        """
        ...

    @abstractmethod
    def list_all(self) -> list[GraphSpec]:
        """List all persisted ``GraphSpec`` rows (including historical).

        Returns:
            A list of all specs, ordered by ``spec_id``.
        """
        ...

    @abstractmethod
    def list_records(self) -> list[GraphSpecRecord]:
        """List only the latest spec record per name (ADR-0040 change 3).

        Returns only the newest ``spec_id`` for each name
        (``MAX(spec_id) GROUP BY name``). Historical specs are accessible
        only via ``get_by_id`` / ``load_by_id``.
        """
        ...

    @abstractmethod
    def get_by_id(self, spec_id: int) -> GraphSpecRecord | None:
        """Return metadata for a persisted graph specification ID."""
        ...

    @abstractmethod
    def delete(self, spec_id: int) -> None:
        """Delete a ``GraphSpec`` by its ``spec_id``.

        Args:
            spec_id: The Snowflake ID of the spec to delete.
        """
        ...


class InMemoryGraphSpecStore(GraphSpecStore):
    """Dictionary-backed graph specification store (immutable specs)."""

    def __init__(self) -> None:
        self._specs: dict[int, GraphSpec] = {}
        self._created_at: dict[int, int] = {}

    def save(self, spec: GraphSpec, spec_id: int | None = None) -> int:
        if spec_id is None:
            spec_id = default_id_generator().generate()
        self._specs[spec_id] = spec
        self._created_at[spec_id] = now_ms()
        return spec_id

    def save_if_changed(self, spec: GraphSpec, spec_id: int | None = None) -> int:
        latest_id = self._latest_spec_id_for_name(spec.name)
        if latest_id is not None:
            existing = self._specs.get(latest_id)
            if existing is not None and existing.model_dump_json() == spec.model_dump_json():
                return latest_id
        return self.save(spec, spec_id)

    def load_by_id(self, spec_id: int) -> GraphSpec | None:
        return self._specs.get(spec_id)

    def load_by_name(self, name: str, version: str = "1.0") -> GraphSpec | None:
        latest_id = self._latest_spec_id_for_name(name)
        if latest_id is None:
            return None
        return self._specs.get(latest_id)

    def list_all(self) -> list[GraphSpec]:
        return [self._specs[sid] for sid in sorted(self._specs)]

    def list_records(self) -> list[GraphSpecRecord]:
        latest_ids = self._latest_spec_ids_per_name()
        return [
            GraphSpecRecord(
                spec_id=sid,
                name=self._specs[sid].name,
                version=self._specs[sid].version,
                created_at=self._created_at[sid],
            )
            for sid in sorted(latest_ids)
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
        self._specs.pop(spec_id, None)
        self._created_at.pop(spec_id, None)

    def _latest_spec_id_for_name(self, name: str) -> int | None:
        candidates = [sid for sid, spec in self._specs.items() if spec.name == name]
        if not candidates:
            return None
        return max(candidates)

    def _latest_spec_ids_per_name(self) -> list[int]:
        by_name: dict[str, int] = {}
        for sid, spec in self._specs.items():
            current = by_name.get(spec.name)
            if current is None or sid > current:
                by_name[spec.name] = sid
        return list(by_name.values())


class SqliteGraphSpecStore(GraphSpecStore):
    """SQLite graph specification store over a caller-owned connection."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._conn = connection
        self._init_schema()

    def _init_schema(self) -> None:
        """Create the ``graph_specs`` table + index if they don't exist.

        Matches ``001_initial.sql`` table 16 (ADR-0040 change 3): no
        ``UNIQUE (name, version)``, no auto-update trigger — rows are
        immutable (write-once on INSERT).
        """
        conn = self._conn
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {_SPEC_TABLE} ("
            f"{_COL_SPEC_ID} INTEGER PRIMARY KEY, "
            f"{_COL_NAME} TEXT NOT NULL, "
            f"{_COL_VERSION} TEXT NOT NULL DEFAULT '1.0', "
            f"{_COL_SPEC_JSON} TEXT NOT NULL, "
            f"{_COL_CREATED_AT} INTEGER NOT NULL, "
            f"{_COL_UPDATED_AT} INTEGER NOT NULL"
            f")"
        )
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{_SPEC_TABLE}_name ON {_SPEC_TABLE} ({_COL_NAME})"
        )
        conn.commit()

    def save(self, spec: GraphSpec, spec_id: int | None = None) -> int:
        if spec_id is None:
            spec_id = default_id_generator().generate()
        ts = now_ms()
        spec_json = spec.model_dump_json()
        self._conn.execute(
            f"INSERT INTO {_SPEC_TABLE} "
            f"({_COL_SPEC_ID}, {_COL_NAME}, {_COL_VERSION}, "
            f"{_COL_SPEC_JSON}, {_COL_CREATED_AT}, {_COL_UPDATED_AT}) "
            f"VALUES (?, ?, ?, ?, ?, ?)",
            (spec_id, spec.name, spec.version, spec_json, ts, ts),
        )
        self._conn.commit()
        return spec_id

    def save_if_changed(self, spec: GraphSpec, spec_id: int | None = None) -> int:
        spec_json = spec.model_dump_json()
        row = self._conn.execute(
            f"SELECT {_COL_SPEC_ID}, {_COL_SPEC_JSON} FROM {_SPEC_TABLE} "
            f"WHERE {_COL_NAME} = ? "
            f"ORDER BY {_COL_SPEC_ID} DESC LIMIT 1",
            (spec.name,),
        ).fetchone()
        if row is not None and row[1] == spec_json:
            return int(row[0])
        return self.save(spec, spec_id)

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
            f"WHERE {_COL_NAME} = ? "
            f"ORDER BY {_COL_SPEC_ID} DESC LIMIT 1",
            (name,),
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
            f"FROM {_SPEC_TABLE} "
            f"WHERE {_COL_SPEC_ID} IN ("
            f"SELECT MAX({_COL_SPEC_ID}) FROM {_SPEC_TABLE} GROUP BY {_COL_NAME}"
            f") "
            f"ORDER BY {_COL_SPEC_ID}"
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
