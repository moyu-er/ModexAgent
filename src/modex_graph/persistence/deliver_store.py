# ruff: noqa: ANN401

"""`DeliverStore` persistence for per-node deliver consumption.

Provides:

- `DeliverConsumptionStatus` — re-exported from `.constants`:
  `STAGED` / `PENDING` / `CONSUMED_PENDING` / `CONSUMED_COMPLETED`.
- `DeliverRecord` — frozen Pydantic value object for one accumulated deliver.
- `DeliverStore` ABC (rule 7: ABC, not Protocol) — per-node consumption
  state machine: `accumulate`, `promote_staged_by_source`, `query_consumable`,
  `mark_consumed`, and `promote_consumed`.
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
_COL_NODE_ID = "node_id"
_COL_NEXT_NODE_ID = "next_node_id"
_COL_SOURCE_NODE_ID = "source_node_id"
_COL_SOURCE_INVOCATION_ID = "source_invocation_id"
_COL_CONSUMED_BY_INVOCATION_ID = "consumed_by_invocation_id"
_COL_CONTENT_JSON = "content_json"
_COL_STATUS = "status"
_COL_CREATED_AT = "created_at"
_COL_UPDATED_AT = "updated_at"


def _encode_content(content: Any) -> str:
    """Serialize deliver content for SQLite storage.

    Handles Pydantic ``BaseModel`` instances (like ``GraphPayload``) by
    wrapping their ``model_dump`` in a typed envelope so reads can
    reconstruct the original type. Raw JSON-native values (dict, str,
    list) are stored directly via ``json.dumps``.
    """
    if isinstance(content, BaseModel):
        return json.dumps(
            {
                "__pydantic__": True,
                "class": type(content).__name__,
                "data": content.model_dump(mode="json"),
            }
        )
    return json.dumps(content, default=str)


def _decode_content(raw: str) -> Any:
    """Deserialize deliver content from SQLite storage.

    Reverses ``_encode_content``: if the parsed JSON is a typed envelope
    (``__pydantic__`` marker), reconstructs the original ``BaseModel``.
    Otherwise returns the parsed value as-is.
    """
    parsed = json.loads(raw)
    if isinstance(parsed, dict) and parsed.get("__pydantic__"):
        cls_name = parsed.get("class", "")
        data = parsed.get("data", {})
        if cls_name == "GraphPayload":
            from ..integration import GraphPayload

            return GraphPayload.model_validate(data)
        return data
    return parsed


class DeliverRecord(BaseModel):
    """One accumulated deliver entry. Frozen value object (rule 12).

    Fields:

    - ``deliver_id: int`` — Snowflake ID (primary key).
    - ``graph_instance_id: int`` — FK -> ``graph_instances``.
    - ``node_id: str`` — the target node that owns the store.
    - ``source_node_id: str`` — the delivering node.
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
    node_id: str = Field(description="The target node that owns the store.")
    source_node_id: str = Field(description="The delivering node.")
    source_invocation_id: int = Field(description="Deliverer's invocation_id.")
    consumed_by_invocation_id: int | None = Field(
        default=None,
        description="Consumer's invocation_id; None until consumed.",
    )
    content: Any = Field(description="Delivered content (JSON-serializable).")
    status: DeliverConsumptionStatus = Field(
        default=DeliverConsumptionStatus.PENDING,
        description="Consumption status (STAGED/PENDING/CONSUMED_PENDING/CONSUMED_COMPLETED).",
    )
    created_at: int = Field(description="Epoch ms.")
    updated_at: int = Field(description="Epoch ms.")


class DeliverStore(ABC):
    """Per-node deliver accumulation + consumption state machine (rule 7: ABC).

    The store is owned by a single node (the
    ``node_id``); delivers are accumulated into it and consumed by that
    node's invocations.

    - ``accumulate`` — keyword-only signature with ``source_node_id`` +
      ``source_invocation_id``.
    - ``query_consumable`` — query delivers ready for consumption.
    - ``mark_consumed`` — mark delivers as consumed by an invocation.
    - ``promote_staged_by_source`` — make a completed source's outputs visible.
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
        node_id: str,
        source_node_id: str,
        source_invocation_id: int,
        content: Any,
        status: DeliverConsumptionStatus = DeliverConsumptionStatus.PENDING,
    ) -> int:
        """Accumulate a deliver into this store. Returns ``deliver_id`` (Snowflake).

        Args:
            graph_instance_id: The graph instance ID (FK -> graph_instances).
            node_id: The owning node ID (this store's owner).
            source_node_id: The delivering node ID.
            source_invocation_id: The deliverer's invocation_id.
            content: The delivered content (JSON-serializable).
            status: Initial visibility and consumption status.

        Returns:
            The ``deliver_id`` (Snowflake ID) of the new record.
        """
        ...

    @abstractmethod
    def query_consumable(self, graph_instance_id: int, node_id: str) -> list[DeliverRecord]:
        """Return delivers ready for consumption by ``node_id``.

        Args:
            graph_instance_id: The graph instance ID.
            node_id: The consuming node's ID.

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
    def promote_staged_by_source(
        self, graph_instance_id: int, source_node_id: str
    ) -> set[str]:
        """Promote a source node's staged delivers and return affected target node IDs."""
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

    `accumulate` stores records in a simple in-memory queue without applying
    a status machine. `mark_consumed` removes matching records, while
    `promote_consumed` and `promote_staged_by_source` are no-ops.
    `query_consumable` returns all remaining records for ``node_id`` regardless
    of their status value.

    Used when the consumption state machine is disabled but the queue
    semantics are still needed (e.g. test harnesses, ephemeral runs).
    """

    def __init__(self) -> None:
        self._records: dict[int, list[DeliverRecord]] = {}

    def accumulate(
        self,
        *,
        graph_instance_id: int,
        node_id: str,
        source_node_id: str,
        source_invocation_id: int,
        content: Any,
        status: DeliverConsumptionStatus = DeliverConsumptionStatus.PENDING,
    ) -> int:
        deliver_id = default_id_generator().generate()
        ts = now_ms()
        record = DeliverRecord(
            deliver_id=deliver_id,
            graph_instance_id=graph_instance_id,
            node_id=node_id,
            source_node_id=source_node_id,
            source_invocation_id=source_invocation_id,
            consumed_by_invocation_id=None,
            content=content,
            status=status,
            created_at=ts,
            updated_at=ts,
        )
        self._records.setdefault(graph_instance_id, []).append(record)
        return deliver_id

    def query_consumable(self, graph_instance_id: int, node_id: str) -> list[DeliverRecord]:
        return [
            record
            for record in self._records.get(graph_instance_id, [])
            if record.node_id == node_id
            and record.status != DeliverConsumptionStatus.STAGED
        ]

    def mark_consumed(self, deliver_ids: list[int], consumed_by_invocation_id: int) -> None:
        if not deliver_ids:
            return
        id_set = set(deliver_ids)
        for gid, records in self._records.items():
            self._records[gid] = [r for r in records if r.deliver_id not in id_set]

    def promote_staged_by_source(
        self, graph_instance_id: int, source_node_id: str
    ) -> set[str]:
        affected_targets: set[str] = set()
        records = self._records.get(graph_instance_id, [])
        timestamp = now_ms()
        for index, record in enumerate(records):
            if (
                record.source_node_id == source_node_id
                and record.status == DeliverConsumptionStatus.STAGED
            ):
                affected_targets.add(record.node_id)
                records[index] = record.model_copy(
                    update={
                        "status": DeliverConsumptionStatus.PENDING,
                        "updated_at": timestamp,
                    }
                )
        return affected_targets

    def promote_consumed(self, consumed_by_invocation_id: int) -> None:
        pass

class InMemoryDeliverStore(DeliverStore):
    """Default in-memory `DeliverStore` — dict keyed by `graph_instance_id`.

    Records are stored in insertion order (Python list order is preserved).
    Uses `default_id_generator()` for Snowflake IDs. Suitable for
    single-process runs and tests. Not persistent across process restarts.

    Consumption state machine (four-state):

    - `accumulate` creates a record with `status = PENDING`.
    - `promote_staged_by_source` makes matching STAGED records PENDING.
    - `query_consumable` returns PENDING and CONSUMED_PENDING records.
    - `mark_consumed` sets `status = CONSUMED_PENDING` + `consumed_by_invocation_id`
      (frozen model — replaces the record in the list via `model_copy`).
    - `promote_consumed` sets matching CONSUMED_PENDING records to
      CONSUMED_COMPLETED without deleting them.
    """

    def __init__(self) -> None:
        self._records: dict[int, list[DeliverRecord]] = {}

    def accumulate(
        self,
        *,
        graph_instance_id: int,
        node_id: str,
        source_node_id: str,
        source_invocation_id: int,
        content: Any,
        status: DeliverConsumptionStatus = DeliverConsumptionStatus.PENDING,
    ) -> int:
        deliver_id = default_id_generator().generate()
        ts = now_ms()
        record = DeliverRecord(
            deliver_id=deliver_id,
            graph_instance_id=graph_instance_id,
            node_id=node_id,
            source_node_id=source_node_id,
            source_invocation_id=source_invocation_id,
            consumed_by_invocation_id=None,
            content=content,
            status=status,
            created_at=ts,
            updated_at=ts,
        )
        self._records.setdefault(graph_instance_id, []).append(record)
        return deliver_id

    def query_consumable(self, graph_instance_id: int, node_id: str) -> list[DeliverRecord]:
        return [
            r
            for r in self._records.get(graph_instance_id, [])
            if r.node_id == node_id
            and r.status
            in (
                DeliverConsumptionStatus.PENDING,
                DeliverConsumptionStatus.CONSUMED_PENDING,
            )
        ]

    def mark_consumed(self, deliver_ids: list[int], consumed_by_invocation_id: int) -> None:
        if not deliver_ids:
            return
        id_set = set(deliver_ids)
        ts = now_ms()
        for records in self._records.values():
            for i, r in enumerate(records):
                if r.deliver_id in id_set and r.status in (
                    DeliverConsumptionStatus.PENDING,
                    DeliverConsumptionStatus.CONSUMED_PENDING,
                ):
                    records[i] = r.model_copy(
                        update={
                            "status": DeliverConsumptionStatus.CONSUMED_PENDING,
                            "consumed_by_invocation_id": consumed_by_invocation_id,
                            "updated_at": ts,
                        }
                    )

    def promote_staged_by_source(
        self, graph_instance_id: int, source_node_id: str
    ) -> set[str]:
        affected_targets: set[str] = set()
        ts = now_ms()
        records = self._records.get(graph_instance_id, [])
        for index, record in enumerate(records):
            if (
                record.source_node_id == source_node_id
                and record.status == DeliverConsumptionStatus.STAGED
            ):
                affected_targets.add(record.node_id)
                records[index] = record.model_copy(
                    update={
                        "status": DeliverConsumptionStatus.PENDING,
                        "updated_at": ts,
                    }
                )
        return affected_targets

    def promote_consumed(self, consumed_by_invocation_id: int) -> None:
        ts = now_ms()
        for records in self._records.values():
            for index, record in enumerate(records):
                if (
                    record.consumed_by_invocation_id == consumed_by_invocation_id
                    and record.status == DeliverConsumptionStatus.CONSUMED_PENDING
                ):
                    records[index] = record.model_copy(
                        update={
                            "status": DeliverConsumptionStatus.CONSUMED_COMPLETED,
                            "updated_at": ts,
                        }
                    )

class SqliteDeliverStore(DeliverStore):
    """SQLite-backed ``DeliverStore`` using stdlib ``sqlite3``.

    Schema is created on construction via ``CREATE TABLE IF NOT EXISTS``
    (lightweight migration — does not depend on modex_agent's
    ``MigrationRunner``). The DDL matches ``001_initial.sql`` table
    ``deliver_states`` (idempotent — if the migration already created it,
    this is a no-op; if ``modex_graph`` is used standalone, this creates it).

    The DDL includes ``source_node_id``,
    ``source_invocation_id``, ``consumed_by_invocation_id`` columns and a
    CHECK constraint allowing all ``DeliverConsumptionStatus`` values.

    The consumption state machine is four-state
    (STAGED → PENDING → CONSUMED_PENDING → CONSUMED_COMPLETED).
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

        Detects old-schema tables (missing ``node_id`` or using the obsolete
        consumption CHECK) and rebuilds them because SQLite ``ALTER TABLE``
        cannot change CHECK or NOT NULL constraints. Complete legacy rows are
        copied into the rebuilt table; obsolete ``consumed`` rows become
        ``consumed_pending``.

        The DDL includes consumption columns
        (``source_node_id``, ``source_invocation_id``,
        ``consumed_by_invocation_id``) and a CHECK constraint allowing all
        ``DeliverConsumptionStatus`` values. Default status is ``'pending'``.
        The ``json_valid(content_json)`` CHECK from the migration is omitted
        here — JSON1 may not be compiled in on all standalone builds; the
        migration includes it for workspace DBs where JSON1 is guaranteed
        (same convention as ``SqliteGraphSpecStore`` and
        ``SqliteNodeStateStore``).
        """
        conn = self._conn
        existing = {
            row[1] for row in conn.execute(f"PRAGMA table_info({_DELIVER_TABLE})").fetchall()
        }
        schema_row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            (_DELIVER_TABLE,),
        ).fetchone()
        table_sql = "" if schema_row is None else str(schema_row[0]).lower()
        current_constraint = (
            f"'{DeliverConsumptionStatus.STAGED.value}'" in table_sql
            and "'consumed'" not in table_sql
        )
        legacy_table: str | None = None
        if existing and (_COL_NODE_ID not in existing or not current_constraint):
            migratable_columns = {
                _COL_DELIVER_ID,
                _COL_GRAPH_INSTANCE_ID,
                _COL_NODE_ID,
                _COL_NEXT_NODE_ID,
                _COL_SOURCE_NODE_ID,
                _COL_SOURCE_INVOCATION_ID,
                _COL_CONSUMED_BY_INVOCATION_ID,
                _COL_CONTENT_JSON,
                _COL_STATUS,
                _COL_CREATED_AT,
                _COL_UPDATED_AT,
            }
            if migratable_columns <= existing:
                legacy_table = f"{_DELIVER_TABLE}_legacy"
                conn.execute(f"ALTER TABLE {_DELIVER_TABLE} RENAME TO {legacy_table}")
            else:
                conn.execute(f"DROP TABLE {_DELIVER_TABLE}")
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {_DELIVER_TABLE} ("
            f"{_COL_DELIVER_ID} INTEGER PRIMARY KEY, "
            f"{_COL_GRAPH_INSTANCE_ID} INTEGER NOT NULL, "
            f"{_COL_NODE_ID} TEXT NOT NULL, "
            f"{_COL_NEXT_NODE_ID} TEXT NOT NULL, "
            f"{_COL_SOURCE_NODE_ID} TEXT NOT NULL DEFAULT '', "
            f"{_COL_SOURCE_INVOCATION_ID} INTEGER NOT NULL DEFAULT 0, "
            f"{_COL_CONSUMED_BY_INVOCATION_ID} INTEGER, "
            f"{_COL_CONTENT_JSON} TEXT NOT NULL, "
            f"{_COL_STATUS} TEXT NOT NULL DEFAULT '{DeliverConsumptionStatus.PENDING.value}' "
            f"CHECK ({_COL_STATUS} IN ("
            f"'{DeliverConsumptionStatus.STAGED.value}', "
            f"'{DeliverConsumptionStatus.PENDING.value}', "
            f"'{DeliverConsumptionStatus.CONSUMED_PENDING.value}', "
            f"'{DeliverConsumptionStatus.CONSUMED_COMPLETED.value}'"
            f")), "
            f"{_COL_CREATED_AT} INTEGER NOT NULL, "
            f"{_COL_UPDATED_AT} INTEGER NOT NULL"
            f")"
        )
        if legacy_table is not None:
            columns = ", ".join(
                (
                    _COL_DELIVER_ID,
                    _COL_GRAPH_INSTANCE_ID,
                    _COL_NODE_ID,
                    _COL_NEXT_NODE_ID,
                    _COL_SOURCE_NODE_ID,
                    _COL_SOURCE_INVOCATION_ID,
                    _COL_CONSUMED_BY_INVOCATION_ID,
                    _COL_CONTENT_JSON,
                    _COL_STATUS,
                    _COL_CREATED_AT,
                    _COL_UPDATED_AT,
                )
            )
            conn.execute(
                f"INSERT INTO {_DELIVER_TABLE} ({columns}) "
                f"SELECT {_COL_DELIVER_ID}, {_COL_GRAPH_INSTANCE_ID}, {_COL_NODE_ID}, "
                f"{_COL_NEXT_NODE_ID}, {_COL_SOURCE_NODE_ID}, {_COL_SOURCE_INVOCATION_ID}, "
                f"{_COL_CONSUMED_BY_INVOCATION_ID}, {_COL_CONTENT_JSON}, "
                f"CASE {_COL_STATUS} WHEN 'consumed' "
                f"THEN '{DeliverConsumptionStatus.CONSUMED_PENDING.value}' ELSE {_COL_STATUS} END, "
                f"{_COL_CREATED_AT}, {_COL_UPDATED_AT} FROM {legacy_table}"
            )
            conn.execute(f"DROP TABLE {legacy_table}")
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{_DELIVER_TABLE}_node "
            f"ON {_DELIVER_TABLE} ({_COL_GRAPH_INSTANCE_ID}, {_COL_NODE_ID}, {_COL_STATUS})"
        )
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{_DELIVER_TABLE}_target "
            f"ON {_DELIVER_TABLE} ({_COL_GRAPH_INSTANCE_ID}, {_COL_NEXT_NODE_ID}, {_COL_STATUS})"
        )
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{_DELIVER_TABLE}_staged_source "
            f"ON {_DELIVER_TABLE} "
            f"({_COL_GRAPH_INSTANCE_ID}, {_COL_SOURCE_NODE_ID}, {_COL_STATUS}) "
            f"WHERE {_COL_STATUS} = '{DeliverConsumptionStatus.STAGED.value}'"
        )
        conn.commit()

    def accumulate(
        self,
        *,
        graph_instance_id: int,
        node_id: str,
        source_node_id: str,
        source_invocation_id: int,
        content: Any,
        status: DeliverConsumptionStatus = DeliverConsumptionStatus.PENDING,
    ) -> int:
        deliver_id = default_id_generator().generate()
        ts = now_ms()
        content_json = _encode_content(content)
        self._conn.execute(
            f"INSERT INTO {_DELIVER_TABLE} "
            f"({_COL_DELIVER_ID}, {_COL_GRAPH_INSTANCE_ID}, {_COL_NODE_ID}, "
            f"{_COL_NEXT_NODE_ID}, {_COL_SOURCE_NODE_ID}, {_COL_SOURCE_INVOCATION_ID}, "
            f"{_COL_CONSUMED_BY_INVOCATION_ID}, {_COL_CONTENT_JSON}, {_COL_STATUS}, "
            f"{_COL_CREATED_AT}, {_COL_UPDATED_AT}) "
            f"VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                deliver_id,
                graph_instance_id,
                node_id,
                node_id,
                source_node_id,
                source_invocation_id,
                None,
                content_json,
                status.value,
                ts,
                ts,
            ),
        )
        self._conn.commit()
        return deliver_id

    def query_consumable(self, graph_instance_id: int, node_id: str) -> list[DeliverRecord]:
        rows = self._conn.execute(
            f"SELECT {_COL_DELIVER_ID}, {_COL_GRAPH_INSTANCE_ID}, {_COL_NODE_ID}, "
            f"{_COL_SOURCE_NODE_ID}, {_COL_SOURCE_INVOCATION_ID}, "
            f"{_COL_CONSUMED_BY_INVOCATION_ID}, {_COL_CONTENT_JSON}, {_COL_STATUS}, "
            f"{_COL_CREATED_AT}, {_COL_UPDATED_AT} "
            f"FROM {_DELIVER_TABLE} "
            f"WHERE {_COL_GRAPH_INSTANCE_ID} = ? AND {_COL_NODE_ID} = ? "
            f"AND {_COL_STATUS} IN (?, ?) "
            f"ORDER BY {_COL_DELIVER_ID}",
            (
                graph_instance_id,
                node_id,
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

    def promote_staged_by_source(
        self, graph_instance_id: int, source_node_id: str
    ) -> set[str]:
        rows = self._conn.execute(
            f"UPDATE {_DELIVER_TABLE} "
            f"SET {_COL_STATUS} = ?, {_COL_UPDATED_AT} = ? "
            f"WHERE {_COL_GRAPH_INSTANCE_ID} = ? AND {_COL_SOURCE_NODE_ID} = ? "
            f"AND {_COL_STATUS} = ? RETURNING {_COL_NODE_ID}",
            (
                DeliverConsumptionStatus.PENDING.value,
                now_ms(),
                graph_instance_id,
                source_node_id,
                DeliverConsumptionStatus.STAGED.value,
            ),
        ).fetchall()
        self._conn.commit()
        return {row[0] for row in rows}

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
            node_id,
            source_node_id,
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
            node_id=node_id,
            source_node_id=source_node_id,
            source_invocation_id=source_invocation_id,
            consumed_by_invocation_id=consumed_by_invocation_id,
            content=_decode_content(content_json),
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
    """Factory for `InMemoryDeliverStore` — in-memory four-state strategy."""

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
