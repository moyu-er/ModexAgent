"""`GraphInstanceStore` — persistence abstraction for `GraphMetadata` records.

Provides:

- `GraphInstanceStore` ABC (rule 7: ABC, not Protocol) — the minimal
  interface for saving and querying `GraphMetadata` records keyed by
  `graph_instance_id` (Snowflake, the persistence unique key that replaces
  `run_id`).
- `InMemoryGraphInstanceStore` — default in-memory dict implementation.
- `SqliteGraphInstanceStore` — SQLite adapter. `CREATE TABLE IF NOT EXISTS
  graph_instances` with the SAME DDL as in `001_initial.sql` table 17
  (idempotent). Field-by-field column mapping (NOT model_dump_json — the
  table has individual columns). Uses `UPDATE ... SET status = ? WHERE
  graph_instance_id = ?` for `update_status`.

The store now stores `GraphMetadata` (the
serializable value object) instead of `GraphInstance` (which is now a
runtime class holding a coordinator — not serializable). Callers that need
a `GraphInstance` wrap the loaded `GraphMetadata` with a coordinator:

    metadata = store.load_by_id(gid)
    if metadata is not None:
        instance = GraphInstance(metadata, create_null_coordinator(gid))

The `graph_instances` SQLite table has individual columns for the basic
identity/status fields. The extra `GraphMetadata` fields (`instance_seq`,
`iteration_count`, `activated_sources`, `pending_dispatches`) are not in
this table — the store fills them with defaults on load. For full-fidelity
metadata persistence, use `GraphMetadataStore` (stores
`metadata_json`).

Follows the EXACT pattern of `dispatch_store.py` / `deliver_store.py`: ABC +
InMemory + SQLite, `now_ms()` from `dispatch_store`, centralized table/column
constants, `CREATE TABLE IF NOT EXISTS`, `?` placeholders,
`check_same_thread=False`, `close()` method.
"""

from __future__ import annotations

import sqlite3
from abc import ABC, abstractmethod
from typing import Any

from .constants import GraphInstanceStatus
from .dispatch_store import now_ms
from .graph_metadata import GraphMetadata

# ── Table / column name constants ─────────────────────────────────────────
# Centralized (rule 14) — same pattern as dispatch_store.py / deliver_store.py.

_INSTANCE_TABLE = "graph_instances"
_COL_GRAPH_INSTANCE_ID = "graph_instance_id"
_COL_SPEC_ID = "spec_id"
_COL_PARENT_INSTANCE_ID = "parent_instance_id"
_COL_PARENT_NODE = "parent_node"
_COL_STATUS = "status"
_COL_CREATED_AT = "created_at"
_COL_UPDATED_AT = "updated_at"

# Allowed status values (matches the CHECK constraint in 001_initial.sql
# table 17). Kept as a module-level constant for parity with the migration.
_ALLOWED_STATUSES = frozenset(
    {"running", "paused", "stopped", "crashed", "completed", "failed"}
)

# Default values for GraphMetadata fields not stored in the graph_instances
# table. The table only has identity + status columns; the scheduler
# bookkeeping fields (instance_seq, iteration_count, activated_sources,
# pending_dispatches) are filled with these defaults on load. For
# full-fidelity metadata, use GraphMetadataStore.
_DEFAULT_INSTANCE_SEQ = 0
_DEFAULT_ITERATION_COUNT = 0
_DEFAULT_ACTIVATED_SOURCES: dict[str, list[str]] = {}
_DEFAULT_PENDING_DISPATCHES: dict[str, dict[str, list[dict[str, Any] | None]]] = {}


class GraphInstanceStore(ABC):
    """Persistence abstraction for `GraphMetadata` records (rule 7: ABC).

    The store is keyed by `graph_instance_id` — a Snowflake ID (BIGINT) that
    is the persistence unique key (replaces the in-memory `run_id`). Parent
    linkage (`parent_instance_id`) enables nested-subgraph queries for
    cleanup. Status queries support fault recovery (e.g. `load_by_status
    ("crashed")`).

    All methods are synchronous. The bot factory / instance manager calls
    these from non-async contexts (startup, crash recovery, cleanup).

    Implementations:

    - `InMemoryGraphInstanceStore` — dict-backed, default.
    - `SqliteGraphInstanceStore` — SQLite file or `:memory:`.
    """

    @abstractmethod
    def save(self, metadata: GraphMetadata) -> None:
        """Save (insert or update) a `GraphMetadata` record.

        `graph_instance_id` is already set on the metadata. If a row with
        this ID exists, it is updated; otherwise a new row is inserted.

        Args:
            metadata: The `GraphMetadata` to persist. The
                `graph_instance_id` field must be set.
        """
        ...

    @abstractmethod
    def load_by_id(self, graph_instance_id: int) -> GraphMetadata | None:
        """Load a `GraphMetadata` by `graph_instance_id`.

        Args:
            graph_instance_id: The Snowflake ID to look up.

        Returns:
            The `GraphMetadata`, or `None` if not found.
        """
        ...

    @abstractmethod
    def load_by_status(self, status: str) -> list[GraphMetadata]:
        """Load all instances with a given status.

        Used for fault recovery (e.g. `status="crashed"` on restart).

        Args:
            status: The lifecycle status to filter by.

        Returns:
            All `GraphMetadata` records with this status.
        """
        ...

    @abstractmethod
    def load_by_parent(self, parent_instance_id: int) -> list[GraphMetadata]:
        """Load all child instances of a parent.

        Used for nested-subgraph cleanup (recursive delete of children).

        Args:
            parent_instance_id: The parent's `graph_instance_id`.

        Returns:
            All `GraphMetadata` records whose `parent_instance_id` matches.
        """
        ...

    @abstractmethod
    def update_status(self, graph_instance_id: int, status: str) -> None:
        """Update only the `status` field (lifecycle transition).

        Args:
            graph_instance_id: The instance to update.
            status: The new status.
        """
        ...

    @abstractmethod
    def delete(self, graph_instance_id: int) -> None:
        """Delete a `GraphMetadata` record by `graph_instance_id`.

        Args:
            graph_instance_id: The Snowflake ID of the instance to delete.
        """
        ...


class InMemoryGraphInstanceStore(GraphInstanceStore):
    """Default in-memory `GraphInstanceStore` — dict keyed by `graph_instance_id`.

    Suitable for single-process runs and tests. Not persistent across
    process restarts.
    """

    def __init__(self) -> None:
        self._instances: dict[int, GraphMetadata] = {}

    def save(self, metadata: GraphMetadata) -> None:
        self._instances[metadata.graph_instance_id] = metadata

    def load_by_id(self, graph_instance_id: int) -> GraphMetadata | None:
        return self._instances.get(graph_instance_id)

    def load_by_status(self, status: str) -> list[GraphMetadata]:
        return [m for m in self._instances.values() if m.status == status]

    def load_by_parent(self, parent_instance_id: int) -> list[GraphMetadata]:
        return [
            m
            for m in self._instances.values()
            if m.parent_instance_id == parent_instance_id
        ]

    def update_status(self, graph_instance_id: int, status: str) -> None:
        existing = self._instances.get(graph_instance_id)
        if existing is None:
            return
        # Frozen model — replace with a new instance with updated status.
        self._instances[graph_instance_id] = existing.model_copy(
            update={"status": status}
        )

    def delete(self, graph_instance_id: int) -> None:
        self._instances.pop(graph_instance_id, None)


class SqliteGraphInstanceStore(GraphInstanceStore):
    """SQLite-backed `GraphInstanceStore` using stdlib `sqlite3`.

    Schema is created on construction via `CREATE TABLE IF NOT EXISTS`
    (lightweight migration). The DDL matches `001_initial.sql` table 17
    (`graph_instances`) — idempotent.

    Table and column names are module-level constants; all data values go
    through `?` parameter placeholders. The `GraphMetadata` is mapped
    field-by-field to individual columns (NOT `model_dump_json` — the table
    has `spec_id`, `parent_instance_id`, `parent_node`, `status` columns for
    column-level queries).

    `save` uses SQLite UPSERT (`INSERT ... ON CONFLICT DO UPDATE`) so the
    same method handles both insert and update by `graph_instance_id` PK.
    `update_status` uses `UPDATE ... SET status = ? WHERE graph_instance_id
    = ?`.

    The `graph_instances` table stores only the basic identity/status
    columns. The extra `GraphMetadata` fields (`instance_seq`,
    `iteration_count`, `activated_sources`, `pending_dispatches`) are
    filled with defaults on load. For full-fidelity metadata, use
    `GraphMetadataStore`.

    Timestamps are epoch milliseconds (`now_ms()`), per ADR-0029.

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
        """Create the `graph_instances` table + indexes if they don't exist.

        The DDL matches `001_initial.sql` table 17. The `status` CHECK
        constraint is included for parity with the migration.
        """
        conn = self._conn
        statuses = ", ".join(f"'{s}'" for s in sorted(_ALLOWED_STATUSES))
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {_INSTANCE_TABLE} ("
            f"{_COL_GRAPH_INSTANCE_ID} INTEGER PRIMARY KEY, "
            f"{_COL_SPEC_ID} INTEGER NOT NULL, "
            f"{_COL_PARENT_INSTANCE_ID} INTEGER, "
            f"{_COL_PARENT_NODE} TEXT, "
            f"{_COL_STATUS} TEXT NOT NULL DEFAULT 'running' "
            f"CHECK ({_COL_STATUS} IN ({statuses})), "
            f"{_COL_CREATED_AT} INTEGER NOT NULL, "
            f"{_COL_UPDATED_AT} INTEGER NOT NULL"
            f")"
        )
        # Indexes matching the migration.
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{_INSTANCE_TABLE}_spec "
            f"ON {_INSTANCE_TABLE} ({_COL_SPEC_ID})"
        )
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{_INSTANCE_TABLE}_parent "
            f"ON {_INSTANCE_TABLE} ({_COL_PARENT_INSTANCE_ID}) "
            f"WHERE {_COL_PARENT_INSTANCE_ID} IS NOT NULL"
        )
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{_INSTANCE_TABLE}_active "
            f"ON {_INSTANCE_TABLE} ({_COL_STATUS}) "
            f"WHERE {_COL_STATUS} IN ('running', 'paused', 'crashed')"
        )
        conn.commit()

    def save(self, metadata: GraphMetadata) -> None:
        ts = now_ms()
        self._conn.execute(
            f"INSERT INTO {_INSTANCE_TABLE} "
            f"({_COL_GRAPH_INSTANCE_ID}, {_COL_SPEC_ID}, "
            f"{_COL_PARENT_INSTANCE_ID}, {_COL_PARENT_NODE}, "
            f"{_COL_STATUS}, {_COL_CREATED_AT}, {_COL_UPDATED_AT}) "
            f"VALUES (?, ?, ?, ?, ?, ?, ?) "
            f"ON CONFLICT({_COL_GRAPH_INSTANCE_ID}) DO UPDATE SET "
            f"{_COL_SPEC_ID} = excluded.{_COL_SPEC_ID}, "
            f"{_COL_PARENT_INSTANCE_ID} = excluded.{_COL_PARENT_INSTANCE_ID}, "
            f"{_COL_PARENT_NODE} = excluded.{_COL_PARENT_NODE}, "
            f"{_COL_STATUS} = excluded.{_COL_STATUS}, "
            f"{_COL_UPDATED_AT} = excluded.{_COL_UPDATED_AT}",
            (
                metadata.graph_instance_id,
                metadata.spec_id,
                metadata.parent_instance_id,
                metadata.parent_node,
                metadata.status,
                ts,
                ts,
            ),
        )
        self._conn.commit()

    def load_by_id(self, graph_instance_id: int) -> GraphMetadata | None:
        row = self._conn.execute(
            f"SELECT {_COL_GRAPH_INSTANCE_ID}, {_COL_SPEC_ID}, "
            f"{_COL_PARENT_INSTANCE_ID}, {_COL_PARENT_NODE}, {_COL_STATUS} "
            f"FROM {_INSTANCE_TABLE} "
            f"WHERE {_COL_GRAPH_INSTANCE_ID} = ?",
            (graph_instance_id,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_metadata(row)

    def load_by_status(self, status: str) -> list[GraphMetadata]:
        rows = self._conn.execute(
            f"SELECT {_COL_GRAPH_INSTANCE_ID}, {_COL_SPEC_ID}, "
            f"{_COL_PARENT_INSTANCE_ID}, {_COL_PARENT_NODE}, {_COL_STATUS} "
            f"FROM {_INSTANCE_TABLE} "
            f"WHERE {_COL_STATUS} = ? "
            f"ORDER BY {_COL_GRAPH_INSTANCE_ID}",
            (status,),
        ).fetchall()
        return [self._row_to_metadata(r) for r in rows]

    def load_by_parent(self, parent_instance_id: int) -> list[GraphMetadata]:
        rows = self._conn.execute(
            f"SELECT {_COL_GRAPH_INSTANCE_ID}, {_COL_SPEC_ID}, "
            f"{_COL_PARENT_INSTANCE_ID}, {_COL_PARENT_NODE}, {_COL_STATUS} "
            f"FROM {_INSTANCE_TABLE} "
            f"WHERE {_COL_PARENT_INSTANCE_ID} = ? "
            f"ORDER BY {_COL_GRAPH_INSTANCE_ID}",
            (parent_instance_id,),
        ).fetchall()
        return [self._row_to_metadata(r) for r in rows]

    def update_status(self, graph_instance_id: int, status: str) -> None:
        self._conn.execute(
            f"UPDATE {_INSTANCE_TABLE} "
            f"SET {_COL_STATUS} = ?, {_COL_UPDATED_AT} = ? "
            f"WHERE {_COL_GRAPH_INSTANCE_ID} = ?",
            (status, now_ms(), graph_instance_id),
        )
        self._conn.commit()

    def delete(self, graph_instance_id: int) -> None:
        self._conn.execute(
            f"DELETE FROM {_INSTANCE_TABLE} "
            f"WHERE {_COL_GRAPH_INSTANCE_ID} = ?",
            (graph_instance_id,),
        )
        self._conn.commit()

    @staticmethod
    def _row_to_metadata(row: tuple[Any, ...]) -> GraphMetadata:
        """Construct a `GraphMetadata` from a DB row.

        The `graph_instances` table stores only the basic identity/status
        columns. The extra `GraphMetadata` fields are filled with defaults.
        For full-fidelity metadata, use `GraphMetadataStore`.
        """
        (
            graph_instance_id,
            spec_id,
            parent_instance_id,
            parent_node,
            status,
        ) = row
        return GraphMetadata(
            graph_instance_id=graph_instance_id,
            spec_id=spec_id,
            parent_instance_id=parent_instance_id,
            parent_node=parent_node,
            status=GraphInstanceStatus(status),
            instance_seq=_DEFAULT_INSTANCE_SEQ,
            iteration_count=_DEFAULT_ITERATION_COUNT,
            activated_sources=_DEFAULT_ACTIVATED_SOURCES,
            pending_dispatches=_DEFAULT_PENDING_DISPATCHES,
        )

    def close(self) -> None:
        """Close the underlying SQLite connection.

        Not part of the `GraphInstanceStore` ABC — concrete resource cleanup
        for the SQLite adapter. Safe to call multiple times.
        """
        self._conn.close()


__all__ = [
    "GraphInstanceStore",
    "InMemoryGraphInstanceStore",
    "SqliteGraphInstanceStore",
]
