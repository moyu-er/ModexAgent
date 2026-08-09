"""`GraphInstanceStore` — single persistence abstraction for `GraphMetadata`.

Provides:

- `GraphInstanceStore` ABC (rule 7: ABC, not Protocol) — the minimal
  interface for saving and querying `GraphMetadata` records keyed by
  `graph_instance_id` (Snowflake, the persistence unique key).
- `NullGraphInstanceStore` — no-op; `load` returns None. Used by
  ``create_null_coordinator`` (Null strategy).
- `InMemoryGraphInstanceStore` — dict-backed default. In-process only.
- `SqliteGraphInstanceStore` — SQLite adapter. `CREATE TABLE IF NOT EXISTS
  graph_instances` with the SAME DDL as `001_initial.sql` table 17
  (idempotent). Field-by-field column mapping for identity/status fields plus
  a JSON column for the node name-to-ID map. Uses `UPDATE ... SET status = ?
  WHERE graph_instance_id = ?` for `update_status`.

The store persists the ``GraphMetadata`` identity/status fields
(``graph_instance_id``, ``spec_id``, ``parent_instance_id``,
``parent_node``, ``status``) as individual columns and ``node_id_map`` as JSON.
Scheduler bookkeeping
(``instance_seq``, ``iteration_count``, ``activated_sources``,
``pending_dispatches``) is NOT persisted — it is derived at recovery time
from the node_states and deliver stores. Callers that need a
``GraphInstance`` wrap the loaded ``GraphMetadata`` with a coordinator:

    metadata = store.load(gid)
    if metadata is not None:
        instance = GraphInstance(metadata, create_null_coordinator(gid))

Follows the same store pattern as `deliver_store.py`: ABC + Null +
InMemory + SQLite, centralized table/column constants, `CREATE TABLE IF
NOT EXISTS`, `?` placeholders. The SQLite adapter takes a caller-owned
`sqlite3.Connection` and never closes it.
"""

from __future__ import annotations

import sqlite3
from abc import ABC, abstractmethod
from typing import Any

from pydantic import TypeAdapter

from ..constants import GraphInstanceStatus
from ._time import now_ms
from .graph_metadata import GraphMetadata

# ── Table / column name constants ─────────────────────────────────────────
_INSTANCE_TABLE = "graph_instances"
_COL_GRAPH_INSTANCE_ID = "graph_instance_id"
_COL_SPEC_ID = "spec_id"
_COL_PARENT_INSTANCE_ID = "parent_instance_id"
_COL_PARENT_NODE = "parent_node"
_COL_STATUS = "status"
_COL_NODE_ID_MAP_JSON = "node_id_map_json"
_COL_CREATED_AT = "created_at"
_COL_UPDATED_AT = "updated_at"

_NODE_ID_MAP_ADAPTER = TypeAdapter(dict[str, str])

# Allowed status values (matches the CHECK constraint in 001_initial.sql
# table 17). Kept as a module-level constant for parity with the migration.
_ALLOWED_STATUSES = frozenset(
    {"pending", "running", "paused", "stopped", "crashed", "completed", "failed"}
)


class GraphInstanceStore(ABC):
    """Persistence abstraction for `GraphMetadata` records (rule 7: ABC).

    The store is keyed by `graph_instance_id` — a Snowflake ID (BIGINT) that
    is the persistence unique key. Parent linkage (`parent_instance_id`)
    enables nested-subgraph queries for cleanup. Status queries support
    fault recovery (e.g. `load_by_status(GraphInstanceStatus.CRASHED)`).

    All methods are synchronous and must be called from the event-loop
    thread only. The caller owns the ``sqlite3.Connection`` and manages its
    lifetime — the store never closes it.

    Implementations:

    - `NullGraphInstanceStore` — no-op; `load` returns None.
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
    def load(self, graph_instance_id: int) -> GraphMetadata | None:
        """Load a `GraphMetadata` by `graph_instance_id`.

        Args:
            graph_instance_id: The Snowflake ID to look up.

        Returns:
            The `GraphMetadata`, or `None` if not found.
        """
        ...

    @abstractmethod
    def load_by_status(self, status: GraphInstanceStatus) -> list[GraphMetadata]:
        """Load all instances with a given status.

        Used for fault recovery (e.g. `status=GraphInstanceStatus.CRASHED`
        on restart).

        Args:
            status: The lifecycle status (enum, not str) to filter by.

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
    def update_status(self, graph_instance_id: int, status: GraphInstanceStatus) -> None:
        """Update only the `status` field (lifecycle transition).

        Args:
            graph_instance_id: The instance to update.
            status: The new lifecycle status (enum, not str).
        """
        ...

    @abstractmethod
    def delete(self, graph_instance_id: int) -> None:
        """Delete a `GraphMetadata` record by `graph_instance_id`.

        Args:
            graph_instance_id: The Snowflake ID of the instance to delete.
        """
        ...


class NullGraphInstanceStore(GraphInstanceStore):
    """No-op `GraphInstanceStore` — `load` returns None, writes are silent.

    The Null strategy for the persistence coordinator: every method is a
    no-op and ``load`` returns ``None`` so callers fall back to a fresh
    default `GraphMetadata`.
    """

    def save(self, metadata: GraphMetadata) -> None:
        pass

    def load(self, graph_instance_id: int) -> GraphMetadata | None:
        return None

    def load_by_status(self, status: GraphInstanceStatus) -> list[GraphMetadata]:
        return []

    def load_by_parent(self, parent_instance_id: int) -> list[GraphMetadata]:
        return []

    def update_status(self, graph_instance_id: int, status: GraphInstanceStatus) -> None:
        pass

    def delete(self, graph_instance_id: int) -> None:
        pass


class InMemoryGraphInstanceStore(GraphInstanceStore):
    """Default in-memory `GraphInstanceStore` — dict keyed by `graph_instance_id`.

    Suitable for single-process runs and tests. Not persistent across
    process restarts.
    """

    def __init__(self) -> None:
        self._instances: dict[int, GraphMetadata] = {}

    def save(self, metadata: GraphMetadata) -> None:
        self._instances[metadata.graph_instance_id] = metadata

    def load(self, graph_instance_id: int) -> GraphMetadata | None:
        return self._instances.get(graph_instance_id)

    def load_by_status(self, status: GraphInstanceStatus) -> list[GraphMetadata]:
        return [m for m in self._instances.values() if m.status == status]

    def load_by_parent(self, parent_instance_id: int) -> list[GraphMetadata]:
        return [m for m in self._instances.values() if m.parent_instance_id == parent_instance_id]

    def update_status(self, graph_instance_id: int, status: GraphInstanceStatus) -> None:
        existing = self._instances.get(graph_instance_id)
        if existing is None:
            return
        # Frozen model — replace with a new instance with updated status.
        self._instances[graph_instance_id] = existing.model_copy(update={"status": status})

    def delete(self, graph_instance_id: int) -> None:
        self._instances.pop(graph_instance_id, None)


class SqliteGraphInstanceStore(GraphInstanceStore):
    """SQLite-backed `GraphInstanceStore` using stdlib `sqlite3`.

    Schema is created on construction via `CREATE TABLE IF NOT EXISTS`
    (lightweight migration). The DDL matches `001_initial.sql` table 17
    (`graph_instances`). The canonical schema lives in the migration
    files; this `_init_schema` is an idempotent fallback for standalone
    `:memory:` / file usage outside the workspace migration runner.

    Table and column names are module-level constants; all data values go
    through `?` parameter placeholders. The 5 `GraphMetadata`
    identity/status fields are mapped field-by-field to individual columns
    (NOT `model_dump_json` — the table has `spec_id`, `parent_instance_id`,
    `parent_node`, `status` columns for column-level queries).

    `save` uses SQLite UPSERT (`INSERT ... ON CONFLICT DO UPDATE`) so the
    same method handles both insert and update by `graph_instance_id` PK.
    `update_status` uses `UPDATE ... SET status = ? WHERE graph_instance_id
    = ?`.

    Timestamps are epoch milliseconds (`now_ms()`), per ADR-0029.

    The store uses a single caller-owned ``sqlite3.Connection`` for its
    lifetime. The caller creates the connection (with ``check_same_thread``
    set as needed) and passes it to all stores sharing one workspace DB;
    the store never closes it.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._conn = connection
        self._init_schema()

    def _init_schema(self) -> None:
        """Create the `graph_instances` table + indexes if they don't exist.

        Detects old-schema tables (missing ``node_id_map_json`` column) and
        rebuilds them from scratch — SQLite ``ALTER TABLE`` cannot change
        CHECK constraints (e.g. adding ``'pending'`` to the status CHECK),
        so a full rebuild is the only correct path. Safe because there is
        no production data to preserve.

        The DDL matches `001_initial.sql` table 17. The `status` CHECK
        constraint is included for parity with the migration.
        """
        conn = self._conn
        existing = {
            row[1] for row in conn.execute(f"PRAGMA table_info({_INSTANCE_TABLE})").fetchall()
        }
        if existing and _COL_NODE_ID_MAP_JSON not in existing:
            conn.execute(f"DROP TABLE IF EXISTS {_INSTANCE_TABLE}")
        statuses = ", ".join(f"'{s}'" for s in sorted(_ALLOWED_STATUSES))
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {_INSTANCE_TABLE} ("
            f"{_COL_GRAPH_INSTANCE_ID} INTEGER PRIMARY KEY, "
            f"{_COL_SPEC_ID} INTEGER NOT NULL, "
            f"{_COL_PARENT_INSTANCE_ID} INTEGER, "
            f"{_COL_PARENT_NODE} TEXT, "
            f"{_COL_STATUS} TEXT NOT NULL DEFAULT 'running' "
            f"CHECK ({_COL_STATUS} IN ({statuses})), "
            f"{_COL_NODE_ID_MAP_JSON} TEXT NOT NULL DEFAULT '{{}}' "
            f"CHECK (json_valid({_COL_NODE_ID_MAP_JSON})), "
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
            f"{_COL_STATUS}, {_COL_NODE_ID_MAP_JSON}, "
            f"{_COL_CREATED_AT}, {_COL_UPDATED_AT}) "
            f"VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            f"ON CONFLICT({_COL_GRAPH_INSTANCE_ID}) DO UPDATE SET "
            f"{_COL_SPEC_ID} = excluded.{_COL_SPEC_ID}, "
            f"{_COL_PARENT_INSTANCE_ID} = excluded.{_COL_PARENT_INSTANCE_ID}, "
            f"{_COL_PARENT_NODE} = excluded.{_COL_PARENT_NODE}, "
            f"{_COL_STATUS} = excluded.{_COL_STATUS}, "
            f"{_COL_NODE_ID_MAP_JSON} = excluded.{_COL_NODE_ID_MAP_JSON}, "
            f"{_COL_UPDATED_AT} = excluded.{_COL_UPDATED_AT}",
            (
                metadata.graph_instance_id,
                metadata.spec_id,
                metadata.parent_instance_id,
                metadata.parent_node,
                metadata.status.value,
                _NODE_ID_MAP_ADAPTER.dump_json(metadata.node_id_map).decode("utf-8"),
                ts,
                ts,
            ),
        )
        self._conn.commit()

    def load(self, graph_instance_id: int) -> GraphMetadata | None:
        row = self._conn.execute(
            f"SELECT {_COL_GRAPH_INSTANCE_ID}, {_COL_SPEC_ID}, "
            f"{_COL_PARENT_INSTANCE_ID}, {_COL_PARENT_NODE}, {_COL_STATUS}, "
            f"{_COL_NODE_ID_MAP_JSON}, {_COL_CREATED_AT}, {_COL_UPDATED_AT} "
            f"FROM {_INSTANCE_TABLE} "
            f"WHERE {_COL_GRAPH_INSTANCE_ID} = ?",
            (graph_instance_id,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_metadata(row)

    def load_by_status(self, status: GraphInstanceStatus) -> list[GraphMetadata]:
        rows = self._conn.execute(
            f"SELECT {_COL_GRAPH_INSTANCE_ID}, {_COL_SPEC_ID}, "
            f"{_COL_PARENT_INSTANCE_ID}, {_COL_PARENT_NODE}, {_COL_STATUS}, "
            f"{_COL_NODE_ID_MAP_JSON}, {_COL_CREATED_AT}, {_COL_UPDATED_AT} "
            f"FROM {_INSTANCE_TABLE} "
            f"WHERE {_COL_STATUS} = ? "
            f"ORDER BY {_COL_GRAPH_INSTANCE_ID}",
            (status.value,),
        ).fetchall()
        return [self._row_to_metadata(r) for r in rows]

    def load_by_parent(self, parent_instance_id: int) -> list[GraphMetadata]:
        rows = self._conn.execute(
            f"SELECT {_COL_GRAPH_INSTANCE_ID}, {_COL_SPEC_ID}, "
            f"{_COL_PARENT_INSTANCE_ID}, {_COL_PARENT_NODE}, {_COL_STATUS}, "
            f"{_COL_NODE_ID_MAP_JSON}, {_COL_CREATED_AT}, {_COL_UPDATED_AT} "
            f"FROM {_INSTANCE_TABLE} "
            f"WHERE {_COL_PARENT_INSTANCE_ID} = ? "
            f"ORDER BY {_COL_GRAPH_INSTANCE_ID}",
            (parent_instance_id,),
        ).fetchall()
        return [self._row_to_metadata(r) for r in rows]

    def update_status(self, graph_instance_id: int, status: GraphInstanceStatus) -> None:
        self._conn.execute(
            f"UPDATE {_INSTANCE_TABLE} "
            f"SET {_COL_STATUS} = ?, {_COL_UPDATED_AT} = ? "
            f"WHERE {_COL_GRAPH_INSTANCE_ID} = ?",
            (status.value, now_ms(), graph_instance_id),
        )
        self._conn.commit()

    def delete(self, graph_instance_id: int) -> None:
        self._conn.execute(
            f"DELETE FROM {_INSTANCE_TABLE} WHERE {_COL_GRAPH_INSTANCE_ID} = ?",
            (graph_instance_id,),
        )
        self._conn.commit()

    @staticmethod
    def _row_to_metadata(row: tuple[Any, ...]) -> GraphMetadata:
        """Construct a `GraphMetadata` from a DB row.

        Identity/status fields come from individual columns; the node ID map
        comes from its typed JSON column.
        """
        (
            graph_instance_id,
            spec_id,
            parent_instance_id,
            parent_node,
            status,
            node_id_map_json,
            created_at,
            updated_at,
        ) = row
        return GraphMetadata(
            graph_instance_id=graph_instance_id,
            spec_id=spec_id,
            parent_instance_id=parent_instance_id,
            parent_node=parent_node,
            status=GraphInstanceStatus(status),
            node_id_map=_NODE_ID_MAP_ADAPTER.validate_json(node_id_map_json),
            created_at=created_at,
            updated_at=updated_at,
        )


__all__ = [
    "GraphInstanceStore",
    "InMemoryGraphInstanceStore",
    "NullGraphInstanceStore",
    "SqliteGraphInstanceStore",
]
