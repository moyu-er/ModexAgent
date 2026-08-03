# ruff: noqa: ANN401

"""`DeliverStore` — persistence abstraction for accumulated delivers (ticket 07).

Provides:

- `DeliverStatus` StrEnum — `ACCUMULATED` / `SUBMITTED` (rule 1: enums
  over raw strings).
- `DeliverRecord` — frozen Pydantic value object: one accumulated deliver
  entry (rules 10-16).
- `DeliverStore` ABC (rule 7: ABC, not Protocol) — the minimal interface
  for accumulating, querying, and marking delivers.
- `InMemoryDeliverStore` — dict-backed default, uses `default_id_generator()`.
- `SqliteDeliverStore` — SQLite adapter. `CREATE TABLE IF NOT EXISTS
  deliver_states` with the SAME DDL as in `001_initial.sql` (idempotent).
  Content serialized via `json.dumps` / `json.loads`. Uses
  `default_id_generator()` for Snowflake IDs.

Follows the EXACT pattern of `dispatch_store.py`: ABC + InMemory + SQLite,
`now_ms()` from `dispatch_store`, centralized table/column constants,
`CREATE TABLE IF NOT EXISTS`, `?` placeholders.

Per ticket 07: `ParallelScheduler` scenario uses `deliver_states` table
(SQLite); `LinearScheduler` scenario can use in-memory objects (no table),
because crash -> re-run `execute` -> delivers re-accumulate.
"""

from __future__ import annotations

import json
import sqlite3
from abc import ABC, abstractmethod
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .dispatch_store import now_ms
from .id_generator import default_id_generator

# ── Table / column name constants ─────────────────────────────────────────
# Centralized (rule 14) — same pattern as dispatch_store.py. The DDL/DML
# statements below are assembled from these constants; all data values go
# through `?` parameter placeholders.

_DELIVER_TABLE = "deliver_states"
_COL_DELIVER_ID = "deliver_id"
_COL_GRAPH_INSTANCE_ID = "graph_instance_id"
_COL_NODE_NAME = "node_name"
_COL_NEXT_NODE = "next_node"
_COL_CONTENT_JSON = "content_json"
_COL_STATUS = "status"
_COL_CREATED_AT = "created_at"
_COL_UPDATED_AT = "updated_at"


class DeliverStatus(StrEnum):
    """Status of a deliver entry (rule 1: enum, not raw string).

    - `ACCUMULATED` — the deliver has been accumulated by `_deliver` but
      not yet dispatched to the downstream node.
    - `SUBMITTED` — the deliver has been dispatched to the downstream node
      by `_submit` (marked via `mark_submitted`).
    """

    ACCUMULATED = "accumulated"
    SUBMITTED = "submitted"


class DeliverRecord(BaseModel):
    """One accumulated deliver entry. Frozen value object (rule 12).

    Fields:

    - `deliver_id: int` — Snowflake ID (primary key).
    - `graph_instance_id: int` — FK -> `graph_instances`.
    - `node_name: str` — the accumulating node.
    - `next_node: str` — target downstream node (or `""` for unresolved).
    - `content: Any` — delivered content (JSON-serializable).
    - `status: DeliverStatus` — `ACCUMULATED` | `SUBMITTED` (default accumulated).
    - `created_at: int` — epoch ms.
    - `updated_at: int` — epoch ms.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    deliver_id: int = Field(description="Snowflake ID (primary key).")
    graph_instance_id: int = Field(description="FK -> graph_instances.")
    node_name: str = Field(description="The accumulating node.")
    next_node: str = Field(description="Target downstream node (or empty for unresolved).")
    content: Any = Field(description="Delivered content (JSON-serializable).")
    status: DeliverStatus = Field(
        default=DeliverStatus.ACCUMULATED,
        description="accumulated | submitted.",
    )
    created_at: int = Field(description="Epoch ms.")
    updated_at: int = Field(description="Epoch ms.")


class DeliverStore(ABC):
    """Persistence abstraction for accumulated delivers (rule 7: ABC).

    The store is keyed by `graph_instance_id` — a 64-bit int identifying
    one graph run (replaces the string `run_id` used by `DispatchStore`;
    per ticket 10, `graph_instance_id` is the persistence unique key).

    All methods are synchronous. `_deliver` runs synchronously inside
    `Node.execute` (called from `deliver()`), so a sync store matches the
    call site.

    Implementations:

    - `InMemoryDeliverStore` — dict-backed, default.
    - `SqliteDeliverStore` — SQLite file or `:memory:`.
    """

    @abstractmethod
    def accumulate(
        self,
        graph_instance_id: int,
        node_name: str,
        next_node: str,
        content: Any,
    ) -> int:
        """Accumulate a deliver. Returns the `deliver_id` (Snowflake).

        Args:
            graph_instance_id: The graph instance ID (FK -> graph_instances).
            node_name: The accumulating node's name.
            next_node: The target downstream node (or `""` for unresolved).
            content: The delivered content (JSON-serializable).

        Returns:
            The `deliver_id` (Snowflake ID) of the new record.
        """
        ...

    @abstractmethod
    def query_pending(self, graph_instance_id: int, node_name: str) -> list[DeliverRecord]:
        """Return all accumulated (not submitted) delivers for `node_name`.

        Args:
            graph_instance_id: The graph instance ID.
            node_name: The accumulating node's name.

        Returns:
            All `DeliverRecord`s with `status == "accumulated"` for this
            node under this graph instance, in insertion order.
        """
        ...

    @abstractmethod
    def query_by_target(self, graph_instance_id: int, next_node: str) -> list[DeliverRecord]:
        """Return all accumulated delivers targeting `next_node`.

        Args:
            graph_instance_id: The graph instance ID.
            next_node: The target downstream node.

        Returns:
            All `DeliverRecord`s with `status == "accumulated"` targeting
            `next_node` under this graph instance, in insertion order.
        """
        ...

    @abstractmethod
    def mark_submitted(self, deliver_ids: list[int]) -> None:
        """Mark delivers as submitted (dispatched to downstream).

        Args:
            deliver_ids: The `deliver_id`s to mark as submitted.
        """
        ...

    @abstractmethod
    def clear(self, graph_instance_id: int) -> None:
        """Delete all delivers for `graph_instance_id`.

        Args:
            graph_instance_id: The graph instance ID to clear.
        """
        ...


class InMemoryDeliverStore(DeliverStore):
    """Default in-memory `DeliverStore` — dict keyed by `graph_instance_id`.

    Records are stored in insertion order (Python list order is preserved).
    Uses `default_id_generator()` for Snowflake IDs. Suitable for
    single-process runs and tests. Not persistent across process restarts.

    For `LinearScheduler` scenarios: crash -> re-run `execute` -> delivers
    re-accumulate, so in-memory is sufficient (per ticket 07).
    """

    def __init__(self) -> None:
        self._records: dict[int, list[DeliverRecord]] = {}

    def accumulate(
        self,
        graph_instance_id: int,
        node_name: str,
        next_node: str,
        content: Any,
    ) -> int:
        deliver_id = default_id_generator().generate()
        ts = now_ms()
        record = DeliverRecord(
            deliver_id=deliver_id,
            graph_instance_id=graph_instance_id,
            node_name=node_name,
            next_node=next_node,
            content=content,
            status=DeliverStatus.ACCUMULATED,
            created_at=ts,
            updated_at=ts,
        )
        self._records.setdefault(graph_instance_id, []).append(record)
        return deliver_id

    def query_pending(self, graph_instance_id: int, node_name: str) -> list[DeliverRecord]:
        return [
            r
            for r in self._records.get(graph_instance_id, [])
            if r.node_name == node_name and r.status == DeliverStatus.ACCUMULATED
        ]

    def query_by_target(self, graph_instance_id: int, next_node: str) -> list[DeliverRecord]:
        return [
            r
            for r in self._records.get(graph_instance_id, [])
            if r.next_node == next_node and r.status == DeliverStatus.ACCUMULATED
        ]

    def mark_submitted(self, deliver_ids: list[int]) -> None:
        if not deliver_ids:
            return
        id_set = set(deliver_ids)
        for records in self._records.values():
            for i, r in enumerate(records):
                if r.deliver_id in id_set:
                    # Frozen model — replace with a new instance with updated status.
                    records[i] = r.model_copy(
                        update={
                            "status": DeliverStatus.SUBMITTED,
                            "updated_at": now_ms(),
                        }
                    )

    def clear(self, graph_instance_id: int) -> None:
        self._records.pop(graph_instance_id, None)


class SqliteDeliverStore(DeliverStore):
    """SQLite-backed `DeliverStore` using stdlib `sqlite3`.

    Schema is created on construction via `CREATE TABLE IF NOT EXISTS`
    (lightweight migration — does not depend on modex_agent's
    `MigrationRunner`). The DDL matches `001_initial.sql` table
    `deliver_states` (idempotent — if the migration already created it,
    this is a no-op; if `modex_graph` is used standalone, this creates it).

    Table and column names are module-level constants; all data values go
    through `?` parameter placeholders (no string interpolation, no SQL
    injection surface).

    The `content` field is serialized to JSON text on write and
    deserialized via `json.loads` on read.

    Timestamps are epoch milliseconds (`now_ms()`), per ADR-0029.

    Uses `default_id_generator()` for Snowflake IDs (the `deliver_id`
    primary key — application-side ID generation, not SQLite AUTOINCREMENT,
    because Snowflake IDs are monotonic across processes).

    The store holds a single `sqlite3.Connection` for its lifetime.
    `check_same_thread=False` allows the connection to be used from the
    event-loop thread or a thread-pool worker. Access is serialized by the
    GIL and the synchronous `_deliver` call site — no concurrent writes.

    For `:memory:` databases, the schema and data live as long as the store
    instance. For file paths, data persists across process restarts.
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._init_schema()

    def _init_schema(self) -> None:
        """Create the `deliver_states` table + indexes if they don't exist.

        The DDL matches `001_initial.sql` table 19. The CHECK constraints
        (`status IN (...)`) are included for parity with the migration.
        `json_valid` CHECK is omitted here (it requires the SQLite JSON1
        extension which may not be compiled in on all builds; the migration
        in `001_initial.sql` includes it for workspace DBs where JSON1 is
        guaranteed).
        """
        conn = self._conn
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {_DELIVER_TABLE} ("
            f"{_COL_DELIVER_ID} INTEGER PRIMARY KEY, "
            f"{_COL_GRAPH_INSTANCE_ID} INTEGER NOT NULL, "
            f"{_COL_NODE_NAME} TEXT NOT NULL, "
            f"{_COL_NEXT_NODE} TEXT NOT NULL, "
            f"{_COL_CONTENT_JSON} TEXT NOT NULL, "
            f"{_COL_STATUS} TEXT NOT NULL DEFAULT '{DeliverStatus.ACCUMULATED.value}' "
            f"CHECK ({_COL_STATUS} IN ('{DeliverStatus.ACCUMULATED.value}', "
            f"'{DeliverStatus.SUBMITTED.value}')), "
            f"{_COL_CREATED_AT} INTEGER NOT NULL, "
            f"{_COL_UPDATED_AT} INTEGER NOT NULL"
            f")"
        )
        # Indexes matching the migration (idx_deliver_states_node + idx_deliver_states_target).
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{_DELIVER_TABLE}_node "
            f"ON {_DELIVER_TABLE} ({_COL_GRAPH_INSTANCE_ID}, {_COL_NODE_NAME}, {_COL_STATUS})"
        )
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{_DELIVER_TABLE}_target "
            f"ON {_DELIVER_TABLE} ({_COL_GRAPH_INSTANCE_ID}, {_COL_NEXT_NODE}, {_COL_STATUS})"
        )
        conn.commit()

    def accumulate(
        self,
        graph_instance_id: int,
        node_name: str,
        next_node: str,
        content: Any,
    ) -> int:
        deliver_id = default_id_generator().generate()
        ts = now_ms()
        content_json = json.dumps(content)
        self._conn.execute(
            f"INSERT INTO {_DELIVER_TABLE} "
            f"({_COL_DELIVER_ID}, {_COL_GRAPH_INSTANCE_ID}, {_COL_NODE_NAME}, "
            f"{_COL_NEXT_NODE}, {_COL_CONTENT_JSON}, {_COL_STATUS}, "
            f"{_COL_CREATED_AT}, {_COL_UPDATED_AT}) "
            f"VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                deliver_id,
                graph_instance_id,
                node_name,
                next_node,
                content_json,
                DeliverStatus.ACCUMULATED,
                ts,
                ts,
            ),
        )
        self._conn.commit()
        return deliver_id

    def query_pending(self, graph_instance_id: int, node_name: str) -> list[DeliverRecord]:
        rows = self._conn.execute(
            f"SELECT {_COL_DELIVER_ID}, {_COL_GRAPH_INSTANCE_ID}, {_COL_NODE_NAME}, "
            f"{_COL_NEXT_NODE}, {_COL_CONTENT_JSON}, {_COL_STATUS}, "
            f"{_COL_CREATED_AT}, {_COL_UPDATED_AT} "
            f"FROM {_DELIVER_TABLE} "
            f"WHERE {_COL_GRAPH_INSTANCE_ID} = ? AND {_COL_NODE_NAME} = ? "
            f"AND {_COL_STATUS} = ? "
            f"ORDER BY {_COL_DELIVER_ID}",
            (graph_instance_id, node_name, DeliverStatus.ACCUMULATED),
        ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def query_by_target(self, graph_instance_id: int, next_node: str) -> list[DeliverRecord]:
        rows = self._conn.execute(
            f"SELECT {_COL_DELIVER_ID}, {_COL_GRAPH_INSTANCE_ID}, {_COL_NODE_NAME}, "
            f"{_COL_NEXT_NODE}, {_COL_CONTENT_JSON}, {_COL_STATUS}, "
            f"{_COL_CREATED_AT}, {_COL_UPDATED_AT} "
            f"FROM {_DELIVER_TABLE} "
            f"WHERE {_COL_GRAPH_INSTANCE_ID} = ? AND {_COL_NEXT_NODE} = ? "
            f"AND {_COL_STATUS} = ? "
            f"ORDER BY {_COL_DELIVER_ID}",
            (graph_instance_id, next_node, DeliverStatus.ACCUMULATED),
        ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def mark_submitted(self, deliver_ids: list[int]) -> None:
        if not deliver_ids:
            return
        placeholders = ",".join("?" for _ in deliver_ids)
        self._conn.execute(
            f"UPDATE {_DELIVER_TABLE} "
            f"SET {_COL_STATUS} = ?, {_COL_UPDATED_AT} = ? "
            f"WHERE {_COL_DELIVER_ID} IN ({placeholders})",
            [DeliverStatus.SUBMITTED, now_ms(), *deliver_ids],
        )
        self._conn.commit()

    def clear(self, graph_instance_id: int) -> None:
        self._conn.execute(
            f"DELETE FROM {_DELIVER_TABLE} WHERE {_COL_GRAPH_INSTANCE_ID} = ?",
            (graph_instance_id,),
        )
        self._conn.commit()

    @staticmethod
    def _row_to_record(row: tuple[Any, ...]) -> DeliverRecord:
        (
            deliver_id,
            graph_instance_id,
            node_name,
            next_node,
            content_json,
            status,
            created_at,
            updated_at,
        ) = row
        return DeliverRecord(
            deliver_id=deliver_id,
            graph_instance_id=graph_instance_id,
            node_name=node_name,
            next_node=next_node,
            content=json.loads(content_json),
            status=status,
            created_at=created_at,
            updated_at=updated_at,
        )

    def close(self) -> None:
        """Close the underlying SQLite connection.

        Not part of the `DeliverStore` ABC — concrete resource cleanup for
        the SQLite adapter. Safe to call multiple times.
        """
        self._conn.close()


__all__ = [
    "DeliverStatus",
    "DeliverRecord",
    "DeliverStore",
    "InMemoryDeliverStore",
    "SqliteDeliverStore",
]
