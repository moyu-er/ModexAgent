# ruff: noqa: ANN401

"""`NodeStateStore` — persistence abstraction for per-node state snapshots (P1C.6).

Provides:

- `NodeStateStore` ABC (rule 7: ABC, not Protocol) — the minimal interface
  for saving and querying per-node state snapshots keyed by
  `(graph_instance_id, node_name, version)`.
- `InMemoryNodeStateStore` — default in-memory implementation. Uses
  `default_id_generator()` for Snowflake IDs.
- `SqliteNodeStateStore` — SQLite adapter. `CREATE TABLE IF NOT EXISTS
  node_states` with the SAME DDL as in `001_initial.sql` table 18
  (idempotent). `state` dict serialized via `json.dumps()` / `json.loads()`.
  Append-only (MVCC): `save` always INSERTs, no UPDATE, no per-instance
  DELETE. Uses `default_id_generator()` for Snowflake IDs.

Follows the EXACT pattern of `dispatch_store.py` / `deliver_store.py`: ABC +
InMemory + SQLite, `now_ms()` from `dispatch_store`, centralized table/column
constants, `CREATE TABLE IF NOT EXISTS`, `?` placeholders,
`check_same_thread=False`, `close()` method.

Per the P0.2 DDL (table 18): `node_states` is append-only — one row per
`(graph_instance_id, node_name, version)`. All versions are retained for
MVCC rollback (no `updated_at`, no trigger, no per-instance delete). The
`clear` method deletes ALL node states for a graph instance (bulk cleanup
when an instance is destroyed).
"""

from __future__ import annotations

import json
import sqlite3
from abc import ABC, abstractmethod
from typing import Any

from .dispatch_store import now_ms
from .id_generator import default_id_generator

# ── Table / column name constants ─────────────────────────────────────────
# Centralized (rule 14) — same pattern as dispatch_store.py / deliver_store.py.

_NODE_STATE_TABLE = "node_states"
_COL_NODE_STATE_ID = "node_state_id"
_COL_GRAPH_INSTANCE_ID = "graph_instance_id"
_COL_NODE_NAME = "node_name"
_COL_VERSION = "version"
_COL_STATE_JSON = "state_json"
_COL_CREATED_AT = "created_at"


class NodeStateStore(ABC):
    """Persistence abstraction for per-node state snapshots (rule 7: ABC).

    The store is keyed by `(graph_instance_id, node_name, version)`. It is
    append-only (MVCC): each `save` inserts a new row with a new
    `node_state_id` (Snowflake) and a caller-supplied `version`. All
    versions are retained — no UPDATE, no per-instance DELETE. The `clear`
    method is the only deletion path (bulk cleanup per graph instance).

    All methods are synchronous. The scheduler / engine calls these from
    non-async contexts (state checkpointing after each node execution).

    Implementations:

    - `InMemoryNodeStateStore` — dict-backed, default.
    - `SqliteNodeStateStore` — SQLite file or `:memory:`.
    """

    @abstractmethod
    def save(
        self,
        graph_instance_id: int,
        node_name: str,
        version: int,
        state: dict[str, Any],
    ) -> int:
        """Save a node state snapshot. Returns the `node_state_id` (Snowflake).

        Always inserts a new row (append-only / MVCC). The caller supplies
        the `version` — a per-`(graph_instance_id, node_name)` monotonic
        counter. The `UNIQUE (graph_instance_id, node_name, version)`
        constraint prevents duplicate versions.

        Args:
            graph_instance_id: The graph instance ID (FK -> graph_instances).
            node_name: The node whose state is being snapshotted.
            version: The MVCC version number (monotonic per node).
            state: The state dict (JSON-serializable).

        Returns:
            The `node_state_id` (Snowflake ID) of the new row.
        """
        ...

    @abstractmethod
    def load_latest(
        self, graph_instance_id: int, node_name: str
    ) -> tuple[dict[str, Any], int] | None:
        """Load the latest state for a node.

        Args:
            graph_instance_id: The graph instance ID.
            node_name: The node name.

        Returns:
            `(state_dict, version)` of the highest-version snapshot, or
            `None` if no snapshots exist for this node.
        """
        ...

    @abstractmethod
    def load_version(
        self,
        graph_instance_id: int,
        node_name: str,
        version: int,
    ) -> dict[str, Any] | None:
        """Load a specific version's state.

        Args:
            graph_instance_id: The graph instance ID.
            node_name: The node name.
            version: The MVCC version to load.

        Returns:
            The `state_dict` for that version, or `None` if not found.
        """
        ...

    @abstractmethod
    def load_all_versions(
        self, graph_instance_id: int, node_name: str
    ) -> list[tuple[dict[str, Any], int]]:
        """Load all versions for a node, ordered by version ASC.

        Args:
            graph_instance_id: The graph instance ID.
            node_name: The node name.

        Returns:
            A list of `(state_dict, version)` tuples, ordered by version
            ascending. Empty list if no snapshots exist.
        """
        ...

    @abstractmethod
    def list_nodes(self, graph_instance_id: int) -> list[str]:
        """List all node names that have state snapshots for a graph instance.

        Args:
            graph_instance_id: The graph instance ID.

        Returns:
            A list of distinct node names. Order is implementation-defined.
        """
        ...

    @abstractmethod
    def clear(self, graph_instance_id: int) -> None:
        """Delete ALL node states for a graph instance (cleanup).

        The only deletion path. Called when a graph instance is destroyed.
        Per-instance deletion is not supported (append-only / MVCC).

        Args:
            graph_instance_id: The graph instance to clear.
        """
        ...


class InMemoryNodeStateStore(NodeStateStore):
    """Default in-memory `NodeStateStore`.

    Stores records in a flat list keyed by `graph_instance_id`. Each record
    is a tuple `(node_state_id, node_name, version, state_dict)`. Uses
    `default_id_generator()` for Snowflake IDs. Suitable for single-process
    runs and tests. Not persistent across process restarts.
    """

    def __init__(self) -> None:
        # graph_instance_id -> list of (node_state_id, node_name, version, state)
        self._records: dict[int, list[tuple[int, str, int, dict[str, Any]]]] = {}

    def save(
        self,
        graph_instance_id: int,
        node_name: str,
        version: int,
        state: dict[str, Any],
    ) -> int:
        records = self._records.get(graph_instance_id, [])
        for (_, existing_name, existing_ver, _) in records:
            if existing_name == node_name and existing_ver == version:
                raise ValueError(
                    f"NodeState for (graph_instance_id={graph_instance_id}, "
                    f"node_name={node_name!r}, version={version}) already exists."
                )
        node_state_id = default_id_generator().generate()
        records.append((node_state_id, node_name, version, state))
        self._records[graph_instance_id] = records
        return node_state_id

    def load_latest(
        self, graph_instance_id: int, node_name: str
    ) -> tuple[dict[str, Any], int] | None:
        records = [
            (state, version)
            for (_, name, version, state) in self._records.get(graph_instance_id, [])
            if name == node_name
        ]
        if not records:
            return None
        return max(records, key=lambda sv: sv[1])

    def load_version(
        self,
        graph_instance_id: int,
        node_name: str,
        version: int,
    ) -> dict[str, Any] | None:
        for (_, name, ver, state) in self._records.get(graph_instance_id, []):
            if name == node_name and ver == version:
                return state
        return None

    def load_all_versions(
        self, graph_instance_id: int, node_name: str
    ) -> list[tuple[dict[str, Any], int]]:
        records = [
            (state, version)
            for (_, name, version, state) in self._records.get(graph_instance_id, [])
            if name == node_name
        ]
        return sorted(records, key=lambda sv: sv[1])

    def list_nodes(self, graph_instance_id: int) -> list[str]:
        seen: dict[str, None] = {}
        for (_, name, _, _) in self._records.get(graph_instance_id, []):
            if name not in seen:
                seen[name] = None
        return list(seen.keys())

    def clear(self, graph_instance_id: int) -> None:
        self._records.pop(graph_instance_id, None)


class SqliteNodeStateStore(NodeStateStore):
    """SQLite-backed `NodeStateStore` using stdlib `sqlite3`.

    Schema is created on construction via `CREATE TABLE IF NOT EXISTS`
    (lightweight migration). The DDL matches `001_initial.sql` table 18
    (`node_states`) — idempotent.

    Table and column names are module-level constants; all data values go
    through `?` parameter placeholders. The `state` dict is serialized via
    `json.dumps()` on write and `json.loads()` on read.

    Append-only (MVCC): `save` always INSERTs (no UPDATE). The
    `UNIQUE (graph_instance_id, node_name, version)` constraint prevents
    duplicate versions. `load_latest` uses `ORDER BY version DESC LIMIT 1`.
    No `updated_at` column, no trigger (append-only table).

    The `json_valid` CHECK constraint from the migration is omitted here
    (same convention as `SqliteDeliverStore` — JSON1 may not be compiled
    in on all standalone builds).

    Timestamps are epoch milliseconds (`now_ms()`), per ADR-0029.

    Uses `default_id_generator()` for Snowflake IDs (the `node_state_id`
    primary key — application-side ID generation, not SQLite AUTOINCREMENT).

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
        """Create the `node_states` table + indexes if they don't exist.

        The DDL matches `001_initial.sql` table 18. The `json_valid` CHECK
        is omitted (same convention as `SqliteDeliverStore`); `UNIQUE
        (graph_instance_id, node_name, version)` is included for parity.
        No `updated_at` column, no trigger (append-only table).
        """
        conn = self._conn
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {_NODE_STATE_TABLE} ("
            f"{_COL_NODE_STATE_ID} INTEGER PRIMARY KEY, "
            f"{_COL_GRAPH_INSTANCE_ID} INTEGER NOT NULL, "
            f"{_COL_NODE_NAME} TEXT NOT NULL, "
            f"{_COL_VERSION} INTEGER NOT NULL DEFAULT 0, "
            f"{_COL_STATE_JSON} TEXT NOT NULL, "
            f"{_COL_CREATED_AT} INTEGER NOT NULL, "
            f"UNIQUE ({_COL_GRAPH_INSTANCE_ID}, {_COL_NODE_NAME}, {_COL_VERSION})"
            f")"
        )
        # Indexes matching the migration.
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{_NODE_STATE_TABLE}_latest "
            f"ON {_NODE_STATE_TABLE} "
            f"({_COL_GRAPH_INSTANCE_ID}, {_COL_NODE_NAME}, {_COL_VERSION} DESC)"
        )
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{_NODE_STATE_TABLE}_node "
            f"ON {_NODE_STATE_TABLE} ({_COL_GRAPH_INSTANCE_ID}, {_COL_NODE_NAME})"
        )
        conn.commit()

    def save(
        self,
        graph_instance_id: int,
        node_name: str,
        version: int,
        state: dict[str, Any],
    ) -> int:
        node_state_id = default_id_generator().generate()
        ts = now_ms()
        state_json = json.dumps(state)
        self._conn.execute(
            f"INSERT INTO {_NODE_STATE_TABLE} "
            f"({_COL_NODE_STATE_ID}, {_COL_GRAPH_INSTANCE_ID}, "
            f"{_COL_NODE_NAME}, {_COL_VERSION}, {_COL_STATE_JSON}, "
            f"{_COL_CREATED_AT}) "
            f"VALUES (?, ?, ?, ?, ?, ?)",
            (node_state_id, graph_instance_id, node_name, version, state_json, ts),
        )
        self._conn.commit()
        return node_state_id

    def load_latest(
        self, graph_instance_id: int, node_name: str
    ) -> tuple[dict[str, Any], int] | None:
        row = self._conn.execute(
            f"SELECT {_COL_STATE_JSON}, {_COL_VERSION} "
            f"FROM {_NODE_STATE_TABLE} "
            f"WHERE {_COL_GRAPH_INSTANCE_ID} = ? AND {_COL_NODE_NAME} = ? "
            f"ORDER BY {_COL_VERSION} DESC LIMIT 1",
            (graph_instance_id, node_name),
        ).fetchone()
        if row is None:
            return None
        state: dict[str, Any] = json.loads(row[0])
        return (state, row[1])

    def load_version(
        self,
        graph_instance_id: int,
        node_name: str,
        version: int,
    ) -> dict[str, Any] | None:
        row = self._conn.execute(
            f"SELECT {_COL_STATE_JSON} "
            f"FROM {_NODE_STATE_TABLE} "
            f"WHERE {_COL_GRAPH_INSTANCE_ID} = ? "
            f"AND {_COL_NODE_NAME} = ? AND {_COL_VERSION} = ?",
            (graph_instance_id, node_name, version),
        ).fetchone()
        if row is None:
            return None
        result: dict[str, Any] = json.loads(row[0])
        return result

    def load_all_versions(
        self, graph_instance_id: int, node_name: str
    ) -> list[tuple[dict[str, Any], int]]:
        rows = self._conn.execute(
            f"SELECT {_COL_STATE_JSON}, {_COL_VERSION} "
            f"FROM {_NODE_STATE_TABLE} "
            f"WHERE {_COL_GRAPH_INSTANCE_ID} = ? AND {_COL_NODE_NAME} = ? "
            f"ORDER BY {_COL_VERSION} ASC",
            (graph_instance_id, node_name),
        ).fetchall()
        results: list[tuple[dict[str, Any], int]] = []
        for r in rows:
            state_dict: dict[str, Any] = json.loads(r[0])
            results.append((state_dict, r[1]))
        return results

    def list_nodes(self, graph_instance_id: int) -> list[str]:
        rows = self._conn.execute(
            f"SELECT DISTINCT {_COL_NODE_NAME} "
            f"FROM {_NODE_STATE_TABLE} "
            f"WHERE {_COL_GRAPH_INSTANCE_ID} = ? "
            f"ORDER BY {_COL_NODE_NAME}",
            (graph_instance_id,),
        ).fetchall()
        return [r[0] for r in rows]

    def clear(self, graph_instance_id: int) -> None:
        self._conn.execute(
            f"DELETE FROM {_NODE_STATE_TABLE} "
            f"WHERE {_COL_GRAPH_INSTANCE_ID} = ?",
            (graph_instance_id,),
        )
        self._conn.commit()

    def close(self) -> None:
        """Close the underlying SQLite connection.

        Not part of the `NodeStateStore` ABC — concrete resource cleanup
        for the SQLite adapter. Safe to call multiple times.
        """
        self._conn.close()


__all__ = [
    "NodeStateStore",
    "InMemoryNodeStateStore",
    "SqliteNodeStateStore",
]
