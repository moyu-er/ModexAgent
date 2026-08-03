"""`GraphSpecStore` — persistence abstraction for `GraphSpec` records (P1C.6).

Provides:

- `GraphSpecStore` ABC (rule 7: ABC, not Protocol) — the minimal interface for
  saving and querying `GraphSpec`s keyed by `spec_id` (Snowflake) and by
  `(name, version)`.
- `InMemoryGraphSpecStore` — default in-memory dict implementation. Uses
  `default_id_generator()` for Snowflake IDs.
- `SqliteGraphSpecStore` — SQLite adapter. `CREATE TABLE IF NOT EXISTS
  graph_specs` with the SAME DDL as in `001_initial.sql` table 16 (idempotent).
  `spec_json` serialized via `GraphSpec.model_dump_json()` /
  `GraphSpec.model_validate_json()`. Uses `default_id_generator()` for
  Snowflake IDs.

Follows the EXACT pattern of `dispatch_store.py` / `deliver_store.py`: ABC +
InMemory + SQLite, `now_ms()` from `dispatch_store`, centralized table/column
constants, `CREATE TABLE IF NOT EXISTS`, `?` placeholders,
`check_same_thread=False`, `close()` method.

Per ticket 08: `GraphSpec` is the declarative, fully-serializable graph
description — the persistence unit. The full chain is
`GraphSpec → GraphSpecCompiler → CompiledGraph → GraphInstance → GraphEngine`.
`GraphSpec` is what gets persisted to the `graph_specs` table; the bot
factory (P3.5) loads it via `GraphSpecStore.load_by_id` or `load_by_name`.
"""

from __future__ import annotations

import sqlite3
from abc import ABC, abstractmethod

from .id_generator import default_id_generator
from .persistence.dispatch_store import now_ms
from .spec import GraphSpec

# ── Table / column name constants ─────────────────────────────────────────
# Centralized (rule 14) — same pattern as dispatch_store.py / deliver_store.py.
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
    """Persistence abstraction for `GraphSpec` records (rule 7: ABC).

    The store is keyed by `spec_id` — a Snowflake ID (BIGINT). A secondary
    unique key `(name, version)` allows lookup by human-readable identifier.

    All methods are synchronous. The bot factory / spec loader calls these
    from non-async contexts (startup, config reload).

    Implementations:

    - `InMemoryGraphSpecStore` — dict-backed, default.
    - `SqliteGraphSpecStore` — SQLite file or `:memory:`.
    """

    @abstractmethod
    def save(self, spec: GraphSpec, spec_id: int | None = None) -> int:
        """Save a `GraphSpec`. If `spec_id` is None, generate a Snowflake ID.

        Args:
            spec: The `GraphSpec` to persist.
            spec_id: Optional Snowflake ID. If `None`, one is generated via
                `default_id_generator()`. If provided, the caller owns
                uniqueness.

        Returns:
            The `spec_id` under which the spec was saved.
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
    def delete(self, spec_id: int) -> None:
        """Delete a `GraphSpec` by `spec_id`.

        Args:
            spec_id: The Snowflake ID of the spec to delete.
        """
        ...


class InMemoryGraphSpecStore(GraphSpecStore):
    """Default in-memory `GraphSpecStore` — dict keyed by `spec_id`.

    A secondary index `(name, version) → spec_id` supports `load_by_name`.
    Uses `default_id_generator()` for Snowflake IDs when `spec_id` is not
    provided. Suitable for single-process runs and tests. Not persistent
    across process restarts.
    """

    def __init__(self) -> None:
        self._specs: dict[int, GraphSpec] = {}
        self._by_name_version: dict[tuple[str, str], int] = {}

    def save(self, spec: GraphSpec, spec_id: int | None = None) -> int:
        if spec_id is None:
            spec_id = default_id_generator().generate()
        key = (spec.name, spec.version)
        if key in self._by_name_version:
            raise ValueError(
                f"GraphSpec with (name={spec.name!r}, version={spec.version!r}) "
                f"already exists (spec_id={self._by_name_version[key]})."
            )
        self._specs[spec_id] = spec
        self._by_name_version[key] = spec_id
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

    def delete(self, spec_id: int) -> None:
        spec = self._specs.pop(spec_id, None)
        if spec is not None:
            self._by_name_version.pop((spec.name, spec.version), None)


class SqliteGraphSpecStore(GraphSpecStore):
    """SQLite-backed `GraphSpecStore` using stdlib `sqlite3`.

    Schema is created on construction via `CREATE TABLE IF NOT EXISTS`
    (lightweight migration — does not depend on modex_agent's
    `MigrationRunner`). The DDL matches `001_initial.sql` table 16
    (`graph_specs`) — idempotent: if the migration already created it, this
    is a no-op; if `modex_graph` is used standalone, this creates the table.

    Table and column names are module-level constants; all data values go
    through `?` parameter placeholders (no string interpolation, no SQL
    injection surface).

    The `GraphSpec` is serialized via `model_dump_json()` on write and
    `model_validate_json()` on read — full Pydantic round-trip.

    The `json_valid` CHECK constraint from the migration is omitted here
    (same convention as `SqliteDeliverStore` — JSON1 may not be compiled
    in on all standalone builds; the migration includes it for workspace
    DBs where JSON1 is guaranteed). The `UNIQUE (name, version)` constraint
    IS included for parity.

    Timestamps are epoch milliseconds (`now_ms()`), per ADR-0029.

    Uses `default_id_generator()` for Snowflake IDs (the `spec_id` primary
    key — application-side ID generation, not SQLite AUTOINCREMENT, because
    Snowflake IDs are monotonic across processes).

    The store holds a single `sqlite3.Connection` for its lifetime.
    `check_same_thread=False` allows the connection to be used from the
    event-loop thread or a thread-pool worker. Access is serialized by the
    GIL and the synchronous call site — no concurrent writes.

    For `:memory:` databases, the schema and data live as long as the store
    instance. For file paths, data persists across process restarts.
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
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
        self._conn.execute(
            f"INSERT INTO {_SPEC_TABLE} "
            f"({_COL_SPEC_ID}, {_COL_NAME}, {_COL_VERSION}, "
            f"{_COL_SPEC_JSON}, {_COL_CREATED_AT}, {_COL_UPDATED_AT}) "
            f"VALUES (?, ?, ?, ?, ?, ?)",
            (spec_id, spec.name, spec.version, spec_json, ts, ts),
        )
        self._conn.commit()
        return spec_id

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

    def delete(self, spec_id: int) -> None:
        self._conn.execute(
            f"DELETE FROM {_SPEC_TABLE} WHERE {_COL_SPEC_ID} = ?",
            (spec_id,),
        )
        self._conn.commit()

    def close(self) -> None:
        """Close the underlying SQLite connection.

        Not part of the `GraphSpecStore` ABC — concrete resource cleanup for
        the SQLite adapter. Safe to call multiple times.
        """
        self._conn.close()


__all__ = [
    "GraphSpecStore",
    "InMemoryGraphSpecStore",
    "SqliteGraphSpecStore",
]
