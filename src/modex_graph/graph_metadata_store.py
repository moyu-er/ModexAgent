"""`GraphMetadataStore` ABC + Null / Memory / Sqlite strategies.

Persistence for `GraphMetadata` (one row per `graph_instance_id`). Three
strategies matching the NodeState / DeliverStore pattern:

- `NullGraphMetadataStore` — every method is a no-op; `load` returns
  None. Used when persistence is disabled.
- `MemoryGraphMetadataStore` — dict-backed default. In-process only.
- `SqliteGraphMetadataStore` — SQLite adapter. Accepts a shared
  `sqlite3.Connection` so multiple stores can share one
  per-workspace SQLite file. Schema: ``graph_metadata`` table with
  ``graph_instance_id`` PK + ``metadata_json`` TEXT + timestamps.
"""

from __future__ import annotations

import sqlite3
from abc import ABC, abstractmethod

from .constants import GraphInstanceStatus
from .dispatch_store import now_ms
from .graph_metadata import GraphMetadata

_GRAPH_METADATA_TABLE = "graph_metadata"
_COL_GM_GRAPH_INSTANCE_ID = "graph_instance_id"
_COL_GM_METADATA_JSON = "metadata_json"
_COL_GM_CREATED_AT = "created_at"
_COL_GM_UPDATED_AT = "updated_at"


class GraphMetadataStore(ABC):
    """Persist graph instance metadata."""

    @abstractmethod
    def save(self, graph_instance_id: int, metadata: GraphMetadata) -> None: ...

    @abstractmethod
    def load(self, graph_instance_id: int) -> GraphMetadata | None: ...

    @abstractmethod
    def update_status(self, graph_instance_id: int, status: GraphInstanceStatus) -> None: ...


class NullGraphMetadataStore(GraphMetadataStore):
    """No-op `GraphMetadataStore` — `load` returns None, writes are silent."""

    def save(self, graph_instance_id: int, metadata: GraphMetadata) -> None:
        pass

    def load(self, graph_instance_id: int) -> GraphMetadata | None:
        return None

    def update_status(self, graph_instance_id: int, status: GraphInstanceStatus) -> None:
        pass


class MemoryGraphMetadataStore(GraphMetadataStore):
    """Dict-backed `GraphMetadataStore` — in-process only.

    `update_status` uses `model_copy(update={...})` on the frozen
    Pydantic model (rule 12 — frozen models are immutable; replacement
    is the only way to update).
    """

    def __init__(self) -> None:
        self._records: dict[int, GraphMetadata] = {}

    def save(self, graph_instance_id: int, metadata: GraphMetadata) -> None:
        self._records[graph_instance_id] = metadata

    def load(self, graph_instance_id: int) -> GraphMetadata | None:
        return self._records.get(graph_instance_id)

    def update_status(self, graph_instance_id: int, status: GraphInstanceStatus) -> None:
        existing = self._records.get(graph_instance_id)
        if existing is None:
            return
        self._records[graph_instance_id] = existing.model_copy(update={"status": status})


class SqliteGraphMetadataStore(GraphMetadataStore):
    """SQLite-backed `GraphMetadataStore`.

    Schema: ``graph_metadata`` table — ``graph_instance_id`` PK +
    ``metadata_json`` TEXT + ``created_at`` / ``updated_at`` INTEGER ms.
    `save` uses `INSERT OR REPLACE` with `metadata.model_dump_json()`.
    `update_status` loads the existing row, applies `model_copy`, and
    re-saves. Accepts a shared connection; the caller owns the
    connection lifetime.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._conn = connection
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.execute(
            f"CREATE TABLE IF NOT EXISTS {_GRAPH_METADATA_TABLE} ("
            f"{_COL_GM_GRAPH_INSTANCE_ID} BIGINT PRIMARY KEY, "
            f"{_COL_GM_METADATA_JSON} TEXT NOT NULL, "
            f"{_COL_GM_CREATED_AT} INTEGER NOT NULL, "
            f"{_COL_GM_UPDATED_AT} INTEGER NOT NULL"
            f")"
        )
        self._conn.commit()

    def save(self, graph_instance_id: int, metadata: GraphMetadata) -> None:
        ts = now_ms()
        self._conn.execute(
            f"INSERT OR REPLACE INTO {_GRAPH_METADATA_TABLE} "
            f"({_COL_GM_GRAPH_INSTANCE_ID}, {_COL_GM_METADATA_JSON}, "
            f"{_COL_GM_CREATED_AT}, {_COL_GM_UPDATED_AT}) "
            f"VALUES (?, ?, ?, ?)",
            (graph_instance_id, metadata.model_dump_json(), ts, ts),
        )
        self._conn.commit()

    def load(self, graph_instance_id: int) -> GraphMetadata | None:
        row = self._conn.execute(
            f"SELECT {_COL_GM_METADATA_JSON} FROM {_GRAPH_METADATA_TABLE} "
            f"WHERE {_COL_GM_GRAPH_INSTANCE_ID} = ?",
            (graph_instance_id,),
        ).fetchone()
        if row is None:
            return None
        return GraphMetadata.model_validate_json(row[0])

    def update_status(self, graph_instance_id: int, status: GraphInstanceStatus) -> None:
        existing = self.load(graph_instance_id)
        if existing is None:
            return
        self.save(graph_instance_id, existing.model_copy(update={"status": status}))


__all__ = [
    "GraphMetadataStore",
    "MemoryGraphMetadataStore",
    "NullGraphMetadataStore",
    "SqliteGraphMetadataStore",
]
