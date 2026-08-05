# ruff: noqa: ANN401

"""`DeliverStore` persistence for per-node deliver consumption.

Provides:

- `DeliverConsumptionStatus` — re-exported from `.constants`:
  `PENDING` / `CONSUMED` / `CONSUMED_PENDING` / `CONSUMED_COMPLETED`.
- `DeliverRecord` — frozen Pydantic value object for one accumulated deliver.
- `DeliverStore` ABC (rule 7: ABC, not Protocol) — per-node consumption
  state machine: `accumulate`, `query_consumable`, `mark_consumed`, and
  `promote_consumed`.
- `InMemoryDeliverStore` — dict-backed default, uses `default_id_generator()`.
- `SqliteDeliverStore` — SQLite adapter for the consumption state machine.
- `DeliverStoreFactory` ABC — `create() -> DeliverStore`.
"""

from __future__ import annotations

import json
import sqlite3
from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..constants import DeliverConsumptionStatus
from ..id_generator import default_id_generator
from ._time import now_ms

# ── Table / column name constants ─────────────────────────────────────────
_DELIVER_TABLE = "deliver_states"
_COL_DELIVER_ID = "deliver_id"
_COL_GRAPH_INSTANCE_ID = "graph_instance_id"
_COL_NODE_NAME = "node_name"
_COL_NEXT_NODE = "next_node"
_COL_SOURCE_NODE = "source_node"
_COL_SOURCE_INVOCATION_ID = "source_invocation_id"
_COL_CONSUMED_BY_INVOCATION_ID = "consumed_by_invocation_id"
_COL_CONTENT_JSON = "content_json"
_COL_STATUS = "status"
_COL_CREATED_AT = "created_at"
_COL_UPDATED_AT = "updated_at"


class DeliverRecord(BaseModel):
    """One accumulated deliver entry. Frozen value object (rule 12).

    Fields:

    - ``deliver_id: int`` — Snowflake ID (primary key).
    - ``graph_instance_id: int`` — FK -> ``graph_instances``.
    - ``node_name: str`` — the target node that owns the store.
    - ``source_node: str`` — the delivering node.
    - ``source_invocation_id: int`` — deliverer's invocation_id.
    - ``consumed_by_invocation_id: int | None`` — consumer's
      invocation_id (None until consumed).
    - ``content: Any`` — delivered content (JSON-serializable).
    - ``status: DeliverConsumptionStatus`` — consumption state machine
      (default PENDING).
    - ``created_at: int`` — epoch ms.
    - ``updated_at: int`` — epoch ms.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    deliver_id: int = Field(description="Snowflake ID (primary key).")
    graph_instance_id: int = Field(description="FK -> graph_instances.")
    node_name: str = Field(description="The target node that owns the store.")
    source_node: str = Field(description="The delivering node.")
    source_invocation_id: int = Field(description="Deliverer's invocation_id.")
    consumed_by_invocation_id: int | None = Field(
        default=None,
        description="Consumer's invocation_id; None until consumed.",
    )
    content: Any = Field(description="Delivered content (JSON-serializable).")
    status: DeliverConsumptionStatus = Field(
        default=DeliverConsumptionStatus.PENDING,
        description="Consumption status (PENDING/CONSUMED/CONSUMED_PENDING/CONSUMED_COMPLETED).",
    )
    created_at: int = Field(description="Epoch ms.")
    updated_at: int = Field(description="Epoch ms.")


class DeliverStore(ABC):
    """Per-node deliver accumulation + consumption state machine (rule 7: ABC).

    The store is owned by a single node (the
    ``target_node``); delivers are accumulated into it and consumed by that
    node's invocations.

    - ``accumulate`` — keyword-only signature with ``source_node`` +
      ``source_invocation_id``.
    - ``query_consumable`` — query delivers ready for consumption.
    - ``mark_consumed`` — mark delivers as consumed by an invocation.
    - ``promote_consumed`` — promote consumed delivers on invocation
      completion.

    The store is keyed by ``graph_instance_id`` — a 64-bit int identifying
    one graph run and serves as the persistence unique key.

    All methods are synchronous and must be called from the event-loop
    thread only. ``_deliver`` runs synchronously inside ``Node.execute``
    (called from ``deliver()``), so a sync store matches the call site.
    The caller owns the ``sqlite3.Connection`` and manages its lifetime —
    the store never closes it.

    Implementations:

    - ``InMemoryDeliverStore`` — dict-backed, default.
    - ``SqliteDeliverStore`` — SQLite file or ``:memory:``.
    """

    @abstractmethod
    def accumulate(
        self,
        *,
        graph_instance_id: int,
        target_node: str,
        source_node: str,
        source_invocation_id: int,
        content: Any,
    ) -> int:
        """Accumulate a deliver into this store. Returns ``deliver_id`` (Snowflake).

        Args:
            graph_instance_id: The graph instance ID (FK -> graph_instances).
            target_node: The owning node (this store's owner).
            source_node: The delivering node.
            source_invocation_id: The deliverer's invocation_id.
            content: The delivered content (JSON-serializable).

        Returns:
            The ``deliver_id`` (Snowflake ID) of the new record.
        """
        ...

    @abstractmethod
    def query_consumable(self, graph_instance_id: int, target_node: str) -> list[DeliverRecord]:
        """Return delivers ready for consumption by ``target_node``.

        Args:
            graph_instance_id: The graph instance ID.
            target_node: The consuming node's name.

        Returns:
            Consumable ``DeliverRecord``s for this node under this graph
            instance, in insertion order.
        """
        ...

    @abstractmethod
    def mark_consumed(self, deliver_ids: list[int], consumed_by_invocation_id: int) -> None:
        """Mark delivers as consumed by an invocation.

        Args:
            deliver_ids: The ``deliver_id``s to mark as consumed.
            consumed_by_invocation_id: The consuming invocation's ID.
        """
        ...

    @abstractmethod
    def promote_consumed(self, consumed_by_invocation_id: int) -> None:
        """Promote consumed delivers on invocation completion.

        Args:
            consumed_by_invocation_id: The invocation whose consumed
                delivers should be promoted.
        """
        ...

class NullDeliverStore(DeliverStore):
    """No-op `DeliverStore` — in-memory queue without consumption state machine.

    `accumulate` creates records with `status = PENDING` and stores them
    in an in-memory queue. `mark_consumed` REMOVES matching records from
    the queue (no CONSUMED state — this is the Null strategy). `promote_consumed`
    is a no-op (no further state transition). `query_consumable` returns
    all remaining records for `target_node`.

    Used when the consumption state machine is disabled but the queue
    semantics are still needed (e.g. test harnesses, ephemeral runs).
    """

    def __init__(self) -> None:
        self._records: dict[int, list[DeliverRecord]] = {}

    def accumulate(
        self,
        *,
        graph_instance_id: int,
        target_node: str,
        source_node: str,
        source_invocation_id: int,
        content: Any,
    ) -> int:
        deliver_id = default_id_generator().generate()
        ts = now_ms()
        record = DeliverRecord(
            deliver_id=deliver_id,
            graph_instance_id=graph_instance_id,
            node_name=target_node,
            source_node=source_node,
            source_invocation_id=source_invocation_id,
            consumed_by_invocation_id=None,
            content=content,
            status=DeliverConsumptionStatus.PENDING,
            created_at=ts,
            updated_at=ts,
        )
        self._records.setdefault(graph_instance_id, []).append(record)
        return deliver_id

    def query_consumable(self, graph_instance_id: int, target_node: str) -> list[DeliverRecord]:
        return [r for r in self._records.get(graph_instance_id, []) if r.node_name == target_node]

    def mark_consumed(self, deliver_ids: list[int], consumed_by_invocation_id: int) -> None:
        if not deliver_ids:
            return
        id_set = set(deliver_ids)
        for gid, records in self._records.items():
            self._records[gid] = [r for r in records if r.deliver_id not in id_set]

    def promote_consumed(self, consumed_by_invocation_id: int) -> None:
        pass

class InMemoryDeliverStore(DeliverStore):
    """Default in-memory `DeliverStore` — dict keyed by `graph_instance_id`.

    Records are stored in insertion order (Python list order is preserved).
    Uses `default_id_generator()` for Snowflake IDs. Suitable for
    single-process runs and tests. Not persistent across process restarts.

    Consumption state machine (two-state):

    - `accumulate` creates a record with `status = PENDING`.
    - `query_consumable` returns records with `status == PENDING`.
    - `mark_consumed` sets `status = CONSUMED` + `consumed_by_invocation_id`
      (frozen model — replaces the record in the list via `model_copy`).
    - `promote_consumed` DELETES records where
      `consumed_by_invocation_id == arg` (two-state: promote = delete).
    """

    def __init__(self) -> None:
        self._records: dict[int, list[DeliverRecord]] = {}

    def accumulate(
        self,
        *,
        graph_instance_id: int,
        target_node: str,
        source_node: str,
        source_invocation_id: int,
        content: Any,
    ) -> int:
        deliver_id = default_id_generator().generate()
        ts = now_ms()
        record = DeliverRecord(
            deliver_id=deliver_id,
            graph_instance_id=graph_instance_id,
            node_name=target_node,
            source_node=source_node,
            source_invocation_id=source_invocation_id,
            consumed_by_invocation_id=None,
            content=content,
            status=DeliverConsumptionStatus.PENDING,
            created_at=ts,
            updated_at=ts,
        )
        self._records.setdefault(graph_instance_id, []).append(record)
        return deliver_id

    def query_consumable(self, graph_instance_id: int, target_node: str) -> list[DeliverRecord]:
        return [
            r
            for r in self._records.get(graph_instance_id, [])
            if r.node_name == target_node and r.status == DeliverConsumptionStatus.PENDING
        ]

    def mark_consumed(self, deliver_ids: list[int], consumed_by_invocation_id: int) -> None:
        if not deliver_ids:
            return
        id_set = set(deliver_ids)
        ts = now_ms()
        for records in self._records.values():
            for i, r in enumerate(records):
                if r.deliver_id in id_set:
                    records[i] = r.model_copy(
                        update={
                            "status": DeliverConsumptionStatus.CONSUMED,
                            "consumed_by_invocation_id": consumed_by_invocation_id,
                            "updated_at": ts,
                        }
                    )

    def promote_consumed(self, consumed_by_invocation_id: int) -> None:
        for gid, records in self._records.items():
            self._records[gid] = [
                r for r in records if r.consumed_by_invocation_id != consumed_by_invocation_id
            ]

class SqliteDeliverStore(DeliverStore):
    """SQLite-backed ``DeliverStore`` using stdlib ``sqlite3``.

    Schema is created on construction via ``CREATE TABLE IF NOT EXISTS``
    (lightweight migration — does not depend on modex_agent's
    ``MigrationRunner``). The DDL matches ``001_initial.sql`` table
    ``deliver_states`` (idempotent — if the migration already created it,
    this is a no-op; if ``modex_graph`` is used standalone, this creates it).

    The DDL includes ``source_node``,
    ``source_invocation_id``, ``consumed_by_invocation_id`` columns and a
    CHECK constraint allowing all ``DeliverConsumptionStatus`` values.

    The consumption state machine is now
    three-state (PENDING → CONSUMED_PENDING → CONSUMED_COMPLETED).
    ``mark_consumed`` transitions PENDING → CONSUMED_PENDING (records
    the ``consumed_by_invocation_id``); ``promote_consumed`` transitions
    CONSUMED_PENDING → CONSUMED_COMPLETED for the given invocation.

    Table and column names are module-level constants; all data values go
    through ``?`` parameter placeholders (no string interpolation, no SQL
    injection surface).

    The ``content`` field is serialized to JSON text on write and
    deserialized via ``json.loads`` on read.

    Timestamps are epoch milliseconds (``now_ms()``), per ADR-0029.

    Uses ``default_id_generator()`` for Snowflake IDs (the ``deliver_id``
    primary key — application-side ID generation, not SQLite AUTOINCREMENT,
    because Snowflake IDs are monotonic across processes).

    The store uses a single caller-owned ``sqlite3.Connection`` for its
    lifetime. The caller creates the connection (with ``check_same_thread``
    set as needed) and passes it to all stores sharing one workspace DB;
    the store never closes it.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._conn = connection
        self._init_schema()

    def _init_schema(self) -> None:
        """Create the ``deliver_states`` table + indexes if they don't exist.

        The DDL includes new columns
        (``source_node``, ``source_invocation_id``,
        ``consumed_by_invocation_id``) and a CHECK constraint allowing all
        ``DeliverConsumptionStatus`` values. Default status is ``'pending'``.

        For existing tables created by old DDL: new columns
        are added via ``ALTER TABLE`` (``_migrate_add_columns``). The
        CHECK constraint on existing tables cannot be altered in SQLite
        — fresh tables get the new CHECK. The ``json_valid(content_json)``
        CHECK from the migration is omitted here — JSON1 may not be
        compiled in on all standalone builds; the migration includes it
        for workspace DBs where JSON1 is guaranteed (same convention as
        ``SqliteGraphSpecStore`` and ``SqliteNodeStateStore``).
        """
        conn = self._conn
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {_DELIVER_TABLE} ("
            f"{_COL_DELIVER_ID} INTEGER PRIMARY KEY, "
            f"{_COL_GRAPH_INSTANCE_ID} INTEGER NOT NULL, "
            f"{_COL_NODE_NAME} TEXT NOT NULL, "
            f"{_COL_NEXT_NODE} TEXT NOT NULL, "
            f"{_COL_SOURCE_NODE} TEXT NOT NULL DEFAULT '', "
            f"{_COL_SOURCE_INVOCATION_ID} INTEGER NOT NULL DEFAULT 0, "
            f"{_COL_CONSUMED_BY_INVOCATION_ID} INTEGER, "
            f"{_COL_CONTENT_JSON} TEXT NOT NULL, "
            f"{_COL_STATUS} TEXT NOT NULL DEFAULT '{DeliverConsumptionStatus.PENDING.value}' "
            f"CHECK ({_COL_STATUS} IN ("
            f"'{DeliverConsumptionStatus.PENDING.value}', "
            f"'{DeliverConsumptionStatus.CONSUMED.value}', "
            f"'{DeliverConsumptionStatus.CONSUMED_PENDING.value}', "
            f"'{DeliverConsumptionStatus.CONSUMED_COMPLETED.value}'"
            f")), "
            f"{_COL_CREATED_AT} INTEGER NOT NULL, "
            f"{_COL_UPDATED_AT} INTEGER NOT NULL"
            f")"
        )
        self._migrate_add_columns()
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

    def _migrate_add_columns(self) -> None:
        """Add consumption columns to existing ``deliver_states`` tables.

        If the table was created by the old DDL, the new
        columns (``source_node``, ``source_invocation_id``,
        ``consumed_by_invocation_id``) won't exist. This adds them via
        ``ALTER TABLE`` with defaults so old rows get backward-compatible
        values. For fresh tables created with the new DDL, this is a
        no-op (columns already present).
        """
        conn = self._conn
        existing = {
            row[1] for row in conn.execute(f"PRAGMA table_info({_DELIVER_TABLE})").fetchall()
        }
        if _COL_SOURCE_NODE not in existing:
            conn.execute(
                f"ALTER TABLE {_DELIVER_TABLE} "
                f"ADD COLUMN {_COL_SOURCE_NODE} TEXT NOT NULL DEFAULT ''"
            )
        if _COL_SOURCE_INVOCATION_ID not in existing:
            conn.execute(
                f"ALTER TABLE {_DELIVER_TABLE} "
                f"ADD COLUMN {_COL_SOURCE_INVOCATION_ID} INTEGER NOT NULL DEFAULT 0"
            )
        if _COL_CONSUMED_BY_INVOCATION_ID not in existing:
            conn.execute(
                f"ALTER TABLE {_DELIVER_TABLE} ADD COLUMN {_COL_CONSUMED_BY_INVOCATION_ID} INTEGER"
            )

    def accumulate(
        self,
        *,
        graph_instance_id: int,
        target_node: str,
        source_node: str,
        source_invocation_id: int,
        content: Any,
    ) -> int:
        deliver_id = default_id_generator().generate()
        ts = now_ms()
        content_json = json.dumps(content)
        self._conn.execute(
            f"INSERT INTO {_DELIVER_TABLE} "
            f"({_COL_DELIVER_ID}, {_COL_GRAPH_INSTANCE_ID}, {_COL_NODE_NAME}, "
            f"{_COL_NEXT_NODE}, {_COL_SOURCE_NODE}, {_COL_SOURCE_INVOCATION_ID}, "
            f"{_COL_CONSUMED_BY_INVOCATION_ID}, {_COL_CONTENT_JSON}, {_COL_STATUS}, "
            f"{_COL_CREATED_AT}, {_COL_UPDATED_AT}) "
            f"VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                deliver_id,
                graph_instance_id,
                target_node,
                "",
                source_node,
                source_invocation_id,
                None,
                content_json,
                DeliverConsumptionStatus.PENDING,
                ts,
                ts,
            ),
        )
        self._conn.commit()
        return deliver_id

    def query_consumable(self, graph_instance_id: int, target_node: str) -> list[DeliverRecord]:
        rows = self._conn.execute(
            f"SELECT {_COL_DELIVER_ID}, {_COL_GRAPH_INSTANCE_ID}, {_COL_NODE_NAME}, "
            f"{_COL_SOURCE_NODE}, {_COL_SOURCE_INVOCATION_ID}, "
            f"{_COL_CONSUMED_BY_INVOCATION_ID}, {_COL_CONTENT_JSON}, {_COL_STATUS}, "
            f"{_COL_CREATED_AT}, {_COL_UPDATED_AT} "
            f"FROM {_DELIVER_TABLE} "
            f"WHERE {_COL_GRAPH_INSTANCE_ID} = ? AND {_COL_NODE_NAME} = ? "
            f"AND {_COL_STATUS} IN (?, ?) "
            f"ORDER BY {_COL_DELIVER_ID}",
            (
                graph_instance_id,
                target_node,
                DeliverConsumptionStatus.PENDING.value,
                DeliverConsumptionStatus.CONSUMED_PENDING.value,
            ),
        ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def mark_consumed(self, deliver_ids: list[int], consumed_by_invocation_id: int) -> None:
        if not deliver_ids:
            return
        placeholders = ",".join("?" for _ in deliver_ids)
        self._conn.execute(
            f"UPDATE {_DELIVER_TABLE} "
            f"SET {_COL_STATUS} = ?, {_COL_CONSUMED_BY_INVOCATION_ID} = ?, "
            f"{_COL_UPDATED_AT} = ? "
            f"WHERE {_COL_DELIVER_ID} IN ({placeholders}) "
            f"AND {_COL_STATUS} IN (?, ?)",
            [
                DeliverConsumptionStatus.CONSUMED_PENDING.value,
                consumed_by_invocation_id,
                now_ms(),
                *deliver_ids,
                DeliverConsumptionStatus.PENDING.value,
                DeliverConsumptionStatus.CONSUMED_PENDING.value,
            ],
        )
        self._conn.commit()

    def promote_consumed(self, consumed_by_invocation_id: int) -> None:
        self._conn.execute(
            f"UPDATE {_DELIVER_TABLE} "
            f"SET {_COL_STATUS} = ?, {_COL_UPDATED_AT} = ? "
            f"WHERE {_COL_CONSUMED_BY_INVOCATION_ID} = ? "
            f"AND {_COL_STATUS} = ?",
            (
                DeliverConsumptionStatus.CONSUMED_COMPLETED.value,
                now_ms(),
                consumed_by_invocation_id,
                DeliverConsumptionStatus.CONSUMED_PENDING.value,
            ),
        )
        self._conn.commit()

    @staticmethod
    def _row_to_record(row: tuple[Any, ...]) -> DeliverRecord:
        (
            deliver_id,
            graph_instance_id,
            node_name,
            source_node,
            source_invocation_id,
            consumed_by_invocation_id,
            content_json,
            status,
            created_at,
            updated_at,
        ) = row
        return DeliverRecord(
            deliver_id=deliver_id,
            graph_instance_id=graph_instance_id,
            node_name=node_name,
            source_node=source_node,
            source_invocation_id=source_invocation_id,
            consumed_by_invocation_id=consumed_by_invocation_id,
            content=json.loads(content_json),
            status=status,
            created_at=created_at,
            updated_at=updated_at,
        )


class DeliverStoreFactory(ABC):
    """Create the default DeliverStore persistence strategy."""

    @abstractmethod
    def create(self) -> DeliverStore: ...


class NullDeliverStoreFactory(DeliverStoreFactory):
    """Factory for `NullDeliverStore` — in-memory queue, no state machine."""

    def create(self) -> NullDeliverStore:
        return NullDeliverStore()


class InMemoryDeliverStoreFactory(DeliverStoreFactory):
    """Factory for `InMemoryDeliverStore` — in-memory two-state strategy."""

    def create(self) -> InMemoryDeliverStore:
        return InMemoryDeliverStore()


class SqliteDeliverStoreFactory(DeliverStoreFactory):
    """Factory for `SqliteDeliverStore` — accepts a shared connection.

    The connection is owned by the caller; the factory does NOT close it.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._conn = connection

    def create(self) -> SqliteDeliverStore:
        return SqliteDeliverStore(self._conn)


__all__ = [
    "DeliverRecord",
    "DeliverStore",
    "DeliverStoreFactory",
    "InMemoryDeliverStore",
    "InMemoryDeliverStoreFactory",
    "NullDeliverStore",
    "NullDeliverStoreFactory",
    "SqliteDeliverStore",
    "SqliteDeliverStoreFactory",
]
