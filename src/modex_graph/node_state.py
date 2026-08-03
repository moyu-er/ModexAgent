# ruff: noqa: ANN401

"""`NodeState` ABC + Null/Simple/Sqlite strategies.

Manages a node's PRIVATE internal state during execution. Distinct from:

- `GraphState` (graph-level, shared across nodes via `ctx.state`) — Pydantic
  state that flows through the graph as the shared "blackboard".
- `NodeStateStore` (persistence layer, SQLite CRUD for the `node_states`
  table) — the on-disk append-only MVCC store keyed by
  `(graph_instance_id, node_name, version)`.

`NodeState` is the IN-MEMORY abstraction a node uses to manage its own
internal state (e.g. `_pending_delivers`, per-execution scratch values,
accumulated tool outputs). Reads/writes hit an in-memory dict first.
Business implementations may optionally sync to `NodeStateStore` for
crash recovery — nodes choose whether to persist by providing a store +
`graph_instance_id` (sync wiring is a business-layer concern; the
framework default `SimpleNodeState` is in-memory only).

`NodeState` is a runtime object with mutable state, so it is NOT a
frozen Pydantic `BaseModel` (rule 12 exception — value-object rule
applies to cross-module structured data, not in-memory runtime caches).
It is an ABC (rule 7: ABC, not Protocol) with `Any` field values
(`# ruff: noqa: ANN401` — field values are node-defined and may be any
type the node chooses to stash, including non-Pydantic runtime objects
like `ToolResult` / `LLMResponse`).

Strategies (Null/Memory/Sqlite):

- `NullNodeState` — every method is a no-op; reads return None / empty.
  Used when persistence is disabled.
- `SimpleNodeState` — in-memory dict + list of `NodeInvocationRecord`.
  Framework default. Suitable for `LinearScheduler` (single-path, no
  concurrent writes to the same node).
- `SqliteNodeState` — SQLite-backed; creates `node_states` table with
  the schema (invocation_id / parent_version / status /
  suspended / updated_at columns) + idempotent migration from legacy
  tables. Accepts a shared `sqlite3.Connection`   (multiple stores
  sharing one connection to avoid multi-file churn).
"""

from __future__ import annotations

import json
import sqlite3
from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, ConfigDict

from .constants import InvocationStatus
from .dispatch_store import now_ms


class NodeInvocationRecord(BaseModel):
    """Persistent record for one node invocation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    invocation_id: int
    graph_instance_id: int
    node_name: str
    version: int
    parent_version: int | None
    status: InvocationStatus
    state_json: dict[str, Any]
    suspended: bool = False
    created_at: int
    updated_at: int


class NodeState(ABC):
    """In-memory per-node state abstraction.

    Manages a node's PRIVATE internal state during execution — distinct
    from `GraphState` (graph-level, shared) and `NodeStateStore`
    (persistence).

    In-memory cache first: reads/writes hit an in-memory dict. Optional
    sync to `NodeStateStore` for persistence (crash recovery) is a
    business-layer concern — nodes choose whether to persist by
    providing a store + `graph_instance_id`.

    Business implementations (`modex_agent`) may provide MVCC,
    multi-version, or stateless variants. `SimpleNodeState` is the
    framework default.

    Contract:

    - `read(field)`: return the stored value. Raises `KeyError` if not
      present (use `has()` to check first if uncertain).
    - `write(field, value)`: store a value (in-memory). Overwrites any
      existing value for the field.
    - `snapshot()`: return a shallow-copy dict of all state (for
      persistence/checkpoint). The caller may mutate the returned dict
      without affecting internal state.
    - `restore(data)`: replace ALL current state with the contents of
      `data`. Prior state is discarded.
    - `has(field)`: return `True` if a field exists (has been written
      or restored).
    """

    @abstractmethod
    def read(self, field: str) -> Any:
        """Read a field value.

        Args:
            field: The field name.

        Returns:
            The stored value (any type the node chose to stash).

        Raises:
            KeyError: If the field is not present. Use `has()` to check
                first if uncertain.
        """
        ...

    @abstractmethod
    def write(self, field: str, value: Any) -> None:
        """Write a field value (in-memory).

        Overwrites any existing value for the field.

        Args:
            field: The field name.
            value: The value (any type).
        """
        ...

    @abstractmethod
    def snapshot(self) -> dict[str, Any]:
        """Snapshot all state as a dict (for persistence/checkpoint).

        Returns a shallow copy — the caller may mutate the returned dict
        without affecting internal state. Deep-copying nested mutable
        values is the caller's responsibility if needed (the framework
        default `SimpleNodeState.snapshot` returns a shallow copy, which
        is sufficient for the common case where field values are
        replaced rather than mutated in place).

        Returns:
            A dict mapping field names to their current values. Empty
            dict if no fields have been written.
        """
        ...

    @abstractmethod
    def restore(self, data: dict[str, Any]) -> None:
        """Restore state from a snapshot dict.

        Replaces ALL current state with the contents of `data`. Prior
        state is discarded. The provided dict is shallow-copied so the
        caller may safely mutate it after `restore` returns.

        Args:
            data: The snapshot dict to restore from.
        """
        ...

    @abstractmethod
    def has(self, field: str) -> bool:
        """Check if a field exists.

        Returns `True` if the field has been written (or restored from a
        snapshot that contained it), `False` otherwise.

        Args:
            field: The field name.

        Returns:
            `True` if the field is present, `False` otherwise.
        """
        ...

    @abstractmethod
    def save_invocation(
        self,
        graph_instance_id: int,
        node_name: str,
        invocation_id: int,
        version: int,
        parent_version: int | None,
        status: InvocationStatus,
        state: dict[str, Any],
        suspended: bool = False,
    ) -> None: ...

    @abstractmethod
    def load_invocation(
        self, graph_instance_id: int, node_name: str, invocation_id: int
    ) -> NodeInvocationRecord | None: ...

    @abstractmethod
    def load_latest(
        self, graph_instance_id: int, node_name: str
    ) -> NodeInvocationRecord | None: ...

    @abstractmethod
    def query_versions(
        self,
        graph_instance_id: int,
        node_name: str,
        status_filter: set[InvocationStatus] | None = None,
    ) -> list[NodeInvocationRecord]: ...

    @abstractmethod
    def load_latest_completed(
        self, graph_instance_id: int, node_name: str
    ) -> NodeInvocationRecord | None: ...


class NullNodeState(NodeState):
    """No-op `NodeState` — every read returns None / empty.

    Used when persistence is disabled (e.g. ephemeral runs that don't
    need crash recovery or per-invocation bookkeeping). All write methods
    are silent no-ops; all read methods return None / empty list / empty
    dict. `has()` always returns False.
    """

    def read(self, field: str) -> Any:
        return None

    def write(self, field: str, value: Any) -> None:
        # No-op.
        pass

    def snapshot(self) -> dict[str, Any]:
        return {}

    def restore(self, data: dict[str, Any]) -> None:
        # No-op.
        pass

    def has(self, field: str) -> bool:
        return False

    def save_invocation(
        self,
        graph_instance_id: int,
        node_name: str,
        invocation_id: int,
        version: int,
        parent_version: int | None,
        status: InvocationStatus,
        state: dict[str, Any],
        suspended: bool = False,
    ) -> None:
        # No-op.
        pass

    def load_invocation(
        self, graph_instance_id: int, node_name: str, invocation_id: int
    ) -> NodeInvocationRecord | None:
        return None

    def load_latest(self, graph_instance_id: int, node_name: str) -> NodeInvocationRecord | None:
        return None

    def query_versions(
        self,
        graph_instance_id: int,
        node_name: str,
        status_filter: set[InvocationStatus] | None = None,
    ) -> list[NodeInvocationRecord]:
        return []

    def load_latest_completed(
        self, graph_instance_id: int, node_name: str
    ) -> NodeInvocationRecord | None:
        return None


class SimpleNodeState(NodeState):
    """Single-state (no MVCC) dict-backed `NodeState`.

    The simplest `NodeState` — a plain dict with
    read/write/snapshot/restore/has. No versioning, no conflict
    detection. Suitable for `LinearScheduler` (single-path, no
    concurrent writes to the same node).

    `save_invocation` / `load_invocation` /
    `load_latest` / `load_latest_completed` / `query_versions` are
    implemented on an in-memory `list[NodeInvocationRecord]` (replacing
    earlier NotImplementedError stubs).

    For `ParallelScheduler` fan-out where the SAME `Node` instance is
    shared across concurrent executions, use a per-instance state wrapper
    or an MVCC implementation (business-layer concern — not provided
    here). Sharing a single `SimpleNodeState` across concurrent
    executions would race.
    """

    def __init__(self, initial: dict[str, Any] | None = None) -> None:
        """Initialize with optional initial state.

        Args:
            initial: Optional initial state dict. Shallow-copied so the
                caller may safely mutate the dict after construction.
                `None` (default) starts with an empty state.
        """
        # Shallow-copy initial to avoid aliasing the caller's dict.
        # Field values themselves are NOT deep-copied — nodes that stash
        # mutable values and share them externally should copy on write.
        self._data: dict[str, Any] = dict(initial) if initial else {}
        self._invocations: list[NodeInvocationRecord] = []

    def read(self, field: str) -> Any:
        if field not in self._data:
            raise KeyError(
                f"NodeState field {field!r} not present. "
                f"Available fields: {sorted(self._data.keys())}"
            )
        return self._data[field]

    def write(self, field: str, value: Any) -> None:
        self._data[field] = value

    def snapshot(self) -> dict[str, Any]:
        # Shallow copy — caller may mutate the returned dict freely.
        # Nested mutable values are shared by reference; the common case
        # (replace-the-field writes) is safe. Deep-copying is the
        # caller's responsibility if they mutate-in-place stashed values.
        return dict(self._data)

    def restore(self, data: dict[str, Any]) -> None:
        # Replace ALL current state with a shallow copy of `data`.
        # Prior state is discarded.
        self._data = dict(data)

    def has(self, field: str) -> bool:
        return field in self._data

    def save_invocation(
        self,
        graph_instance_id: int,
        node_name: str,
        invocation_id: int,
        version: int,
        parent_version: int | None,
        status: InvocationStatus,
        state: dict[str, Any],
        suspended: bool = False,
    ) -> None:
        ts = now_ms()
        # UPSERT: replace any existing record with the same
        # (graph_instance_id, node_name, version). This supports the
        # coordinator's lifecycle transitions (PENDING → RUNNING →
        # COMPLETED, etc.) which save multiple status updates for the
        # same version. created_at is preserved from the original record
        # (matching SqliteNodeState's ON CONFLICT behavior where
        # created_at is excluded from DO UPDATE SET).
        existing_created_at = ts
        for r in self._invocations:
            if (
                r.graph_instance_id == graph_instance_id
                and r.node_name == node_name
                and r.version == version
            ):
                existing_created_at = r.created_at
                break
        record = NodeInvocationRecord(
            invocation_id=invocation_id,
            graph_instance_id=graph_instance_id,
            node_name=node_name,
            version=version,
            parent_version=parent_version,
            status=status,
            state_json=state,
            suspended=suspended,
            created_at=existing_created_at,
            updated_at=ts,
        )
        self._invocations = [
            r
            for r in self._invocations
            if not (
                r.graph_instance_id == graph_instance_id
                and r.node_name == node_name
                and r.version == version
            )
        ]
        self._invocations.append(record)

    def load_invocation(
        self, graph_instance_id: int, node_name: str, invocation_id: int
    ) -> NodeInvocationRecord | None:
        for record in self._invocations:
            if (
                record.graph_instance_id == graph_instance_id
                and record.node_name == node_name
                and record.invocation_id == invocation_id
            ):
                return record
        return None

    def load_latest(self, graph_instance_id: int, node_name: str) -> NodeInvocationRecord | None:
        matching = [
            r
            for r in self._invocations
            if r.graph_instance_id == graph_instance_id and r.node_name == node_name
        ]
        if not matching:
            return None
        return max(matching, key=lambda r: r.invocation_id)

    def query_versions(
        self,
        graph_instance_id: int,
        node_name: str,
        status_filter: set[InvocationStatus] | None = None,
    ) -> list[NodeInvocationRecord]:
        result = [
            r
            for r in self._invocations
            if r.graph_instance_id == graph_instance_id
            and r.node_name == node_name
            and (status_filter is None or r.status in status_filter)
        ]
        result.sort(key=lambda r: r.version, reverse=True)
        return result

    def load_latest_completed(
        self, graph_instance_id: int, node_name: str
    ) -> NodeInvocationRecord | None:
        matching = [
            r
            for r in self._invocations
            if r.graph_instance_id == graph_instance_id
            and r.node_name == node_name
            and r.status == InvocationStatus.COMPLETED
        ]
        if not matching:
            return None
        return max(matching, key=lambda r: r.invocation_id)


# ── SQLite table / column name constants ──────────────────────────────────
# Centralized (rule 14) — same pattern as dispatch_store.py / deliver_store.py.
# The DDL/DML statements below are assembled from these constants; all data
# values go through `?` parameter placeholders.

_NODE_STATES_TABLE = "node_states"
_COL_NODE_STATE_ID = "node_state_id"
_COL_NS_GRAPH_INSTANCE_ID = "graph_instance_id"
_COL_NS_NODE_NAME = "node_name"
_COL_NS_VERSION = "version"
_COL_NS_PARENT_VERSION = "parent_version"
_COL_NS_STATUS = "status"
_COL_NS_INVOCATION_ID = "invocation_id"
_COL_NS_STATE_JSON = "state_json"
_COL_NS_SUSPENDED = "suspended"
_COL_NS_CREATED_AT = "created_at"
_COL_NS_UPDATED_AT = "updated_at"


class SqliteNodeState(NodeState):
    """SQLite-backed `NodeState` — upsert-per-version `node_states` table.

    Schema:

    - ``node_state_id BIGINT PRIMARY KEY`` — Snowflake ID.
    - ``graph_instance_id BIGINT NOT NULL`` — FK -> ``graph_instances``.
    - ``node_name TEXT NOT NULL`` — the node owning this state.
    - ``version INTEGER NOT NULL`` — MVCC version (one row per version).
    - ``parent_version INTEGER`` — nullable parent version (None for v0).
    - ``status TEXT NOT NULL DEFAULT 'pending' CHECK(...)`` —
      ``InvocationStatus`` value (one of pending / running / completed /
      canceled / crashed / superseded).
    - ``invocation_id BIGINT NOT NULL DEFAULT 0`` — the invocation that
      produced this version.
    - ``state_json TEXT NOT NULL`` — JSON-serialized state dict.
    - ``suspended INTEGER NOT NULL DEFAULT 0`` — 0/1 bool flag.
    - ``created_at INTEGER NOT NULL`` / ``updated_at INTEGER NOT NULL`` —
      epoch ms.
    - ``UNIQUE (graph_instance_id, node_name, version)`` — one row per
      version.

    Indexes: ``idx_node_states_latest`` (gid, node, version DESC),
    ``idx_node_states_status`` (gid, node, status),
    ``idx_node_states_cross`` (gid, node, invocation_id),
    ``idx_node_states_global`` (gid, invocation_id DESC).

    For tables created by the legacy schema (no
    ``invocation_id`` / ``parent_version`` / ``status`` / ``suspended`` /
    ``updated_at`` columns), ``_migrate_schema()`` adds them via
    ``ALTER TABLE ADD COLUMN``. Idempotent: re-running is a no-op.

    Accepts a shared ``sqlite3.Connection`` so multiple stores
    (NodeState / GraphMetadata / Deliver) can share one connection to a
    per-workspace SQLite file. The connection is NOT closed by this
    store — the caller manages its lifetime.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._conn = connection
        self._init_schema()

    def _init_schema(self) -> None:
        """Create the ``node_states`` table + indexes; migrate legacy tables.

        Order matters: ``CREATE TABLE IF NOT EXISTS`` is a no-op when the
        legacy table exists, so ``_migrate_schema()`` must run BEFORE index
        creation — the indexes reference columns (``status``,
        ``invocation_id``) that are only added by the migration on legacy
        tables. The SQLite CHECK constraint on an existing table cannot be
        altered in-place — fresh tables get the full CHECK; legacy tables
        get the columns but no CHECK (acceptable: ``InvocationStatus`` is
        enforced at the Python layer via the enum).
        """
        conn = self._conn
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {_NODE_STATES_TABLE} ("
            f"{_COL_NODE_STATE_ID} BIGINT PRIMARY KEY, "
            f"{_COL_NS_GRAPH_INSTANCE_ID} BIGINT NOT NULL, "
            f"{_COL_NS_NODE_NAME} TEXT NOT NULL, "
            f"{_COL_NS_VERSION} INTEGER NOT NULL, "
            f"{_COL_NS_PARENT_VERSION} INTEGER, "
            f"{_COL_NS_STATUS} TEXT NOT NULL DEFAULT '{InvocationStatus.PENDING.value}' "
            f"CHECK ({_COL_NS_STATUS} IN ("
            f"'{InvocationStatus.PENDING.value}', "
            f"'{InvocationStatus.RUNNING.value}', "
            f"'{InvocationStatus.COMPLETED.value}', "
            f"'{InvocationStatus.CANCELED.value}', "
            f"'{InvocationStatus.CRASHED.value}', "
            f"'{InvocationStatus.SUPERSEDED.value}'"
            f")), "
            f"{_COL_NS_INVOCATION_ID} BIGINT NOT NULL DEFAULT 0, "
            f"{_COL_NS_STATE_JSON} TEXT NOT NULL, "
            f"{_COL_NS_SUSPENDED} INTEGER NOT NULL DEFAULT 0, "
            f"{_COL_NS_CREATED_AT} INTEGER NOT NULL, "
            f"{_COL_NS_UPDATED_AT} INTEGER NOT NULL, "
            f"UNIQUE ({_COL_NS_GRAPH_INSTANCE_ID}, {_COL_NS_NODE_NAME}, {_COL_NS_VERSION})"
            f")"
        )
        self._migrate_schema()
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_node_states_latest "
            f"ON {_NODE_STATES_TABLE} "
            f"({_COL_NS_GRAPH_INSTANCE_ID}, {_COL_NS_NODE_NAME}, {_COL_NS_VERSION} DESC)"
        )
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_node_states_status "
            f"ON {_NODE_STATES_TABLE} "
            f"({_COL_NS_GRAPH_INSTANCE_ID}, {_COL_NS_NODE_NAME}, {_COL_NS_STATUS})"
        )
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_node_states_cross "
            f"ON {_NODE_STATES_TABLE} "
            f"({_COL_NS_GRAPH_INSTANCE_ID}, {_COL_NS_NODE_NAME}, {_COL_NS_INVOCATION_ID})"
        )
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_node_states_global "
            f"ON {_NODE_STATES_TABLE} "
            f"({_COL_NS_GRAPH_INSTANCE_ID}, {_COL_NS_INVOCATION_ID} DESC)"
        )
        conn.commit()

    def _migrate_schema(self) -> None:
        """Add columns to legacy ``node_states`` tables.

        Idempotent: re-running on a table that already has the columns
        is a no-op. Adds ``invocation_id`` / ``parent_version`` /
        ``status`` / ``suspended`` / ``updated_at`` to tables created by
        the legacy DDL.
        """
        conn = self._conn
        existing = {
            row[1] for row in conn.execute(f"PRAGMA table_info({_NODE_STATES_TABLE})").fetchall()
        }
        if _COL_NS_INVOCATION_ID not in existing:
            conn.execute(
                f"ALTER TABLE {_NODE_STATES_TABLE} "
                f"ADD COLUMN {_COL_NS_INVOCATION_ID} BIGINT NOT NULL DEFAULT 0"
            )
        if _COL_NS_PARENT_VERSION not in existing:
            conn.execute(
                f"ALTER TABLE {_NODE_STATES_TABLE} ADD COLUMN {_COL_NS_PARENT_VERSION} INTEGER"
            )
        if _COL_NS_STATUS not in existing:
            conn.execute(
                f"ALTER TABLE {_NODE_STATES_TABLE} "
                f"ADD COLUMN {_COL_NS_STATUS} TEXT NOT NULL DEFAULT "
                f"'{InvocationStatus.PENDING.value}'"
            )
        if _COL_NS_SUSPENDED not in existing:
            conn.execute(
                f"ALTER TABLE {_NODE_STATES_TABLE} "
                f"ADD COLUMN {_COL_NS_SUSPENDED} INTEGER NOT NULL DEFAULT 0"
            )
        if _COL_NS_UPDATED_AT not in existing:
            conn.execute(
                f"ALTER TABLE {_NODE_STATES_TABLE} "
                f"ADD COLUMN {_COL_NS_UPDATED_AT} INTEGER NOT NULL DEFAULT 0"
            )
        conn.commit()

    # ── Old in-memory API (dict shim) ────────────────────────────────────
    # SqliteNodeState is primarily the invocation version-chain store. The
    # old read/write/snapshot/restore/has API operates on an in-memory dict
    # that is NOT persisted — kept for ABC conformance (rule 7: every
    # NodeState subclass must implement the full ABC). For SqliteNodeState
    # the in-memory cache is incidental; the source of truth is the table.

    def read(self, field: str) -> Any:
        return None

    def write(self, field: str, value: Any) -> None:
        pass

    def snapshot(self) -> dict[str, Any]:
        return {}

    def restore(self, data: dict[str, Any]) -> None:
        pass

    def has(self, field: str) -> bool:
        return False

    def save_invocation(
        self,
        graph_instance_id: int,
        node_name: str,
        invocation_id: int,
        version: int,
        parent_version: int | None,
        status: InvocationStatus,
        state: dict[str, Any],
        suspended: bool = False,
    ) -> None:
        ts = now_ms()
        from .id_generator import default_id_generator

        node_state_id = default_id_generator().generate()
        self._conn.execute(
            f"INSERT INTO {_NODE_STATES_TABLE} "
            f"({_COL_NODE_STATE_ID}, {_COL_NS_GRAPH_INSTANCE_ID}, {_COL_NS_NODE_NAME}, "
            f"{_COL_NS_VERSION}, {_COL_NS_PARENT_VERSION}, {_COL_NS_STATUS}, "
            f"{_COL_NS_INVOCATION_ID}, {_COL_NS_STATE_JSON}, {_COL_NS_SUSPENDED}, "
            f"{_COL_NS_CREATED_AT}, {_COL_NS_UPDATED_AT}) "
            f"VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            f"ON CONFLICT({_COL_NS_GRAPH_INSTANCE_ID}, {_COL_NS_NODE_NAME}, {_COL_NS_VERSION}) "
            f"DO UPDATE SET "
            f"{_COL_NS_INVOCATION_ID}=excluded.{_COL_NS_INVOCATION_ID}, "
            f"{_COL_NS_PARENT_VERSION}=excluded.{_COL_NS_PARENT_VERSION}, "
            f"{_COL_NS_STATUS}=excluded.{_COL_NS_STATUS}, "
            f"{_COL_NS_STATE_JSON}=excluded.{_COL_NS_STATE_JSON}, "
            f"{_COL_NS_SUSPENDED}=excluded.{_COL_NS_SUSPENDED}, "
            f"{_COL_NS_UPDATED_AT}=excluded.{_COL_NS_UPDATED_AT}",
            (
                node_state_id,
                graph_instance_id,
                node_name,
                version,
                parent_version,
                status.value,
                invocation_id,
                json.dumps(state),
                1 if suspended else 0,
                ts,
                ts,
            ),
        )
        self._conn.commit()

    def load_invocation(
        self, graph_instance_id: int, node_name: str, invocation_id: int
    ) -> NodeInvocationRecord | None:
        row = self._conn.execute(
            f"SELECT {_COL_NODE_STATE_ID}, {_COL_NS_GRAPH_INSTANCE_ID}, "
            f"{_COL_NS_NODE_NAME}, {_COL_NS_VERSION}, {_COL_NS_PARENT_VERSION}, "
            f"{_COL_NS_STATUS}, {_COL_NS_INVOCATION_ID}, {_COL_NS_STATE_JSON}, "
            f"{_COL_NS_SUSPENDED}, {_COL_NS_CREATED_AT}, {_COL_NS_UPDATED_AT} "
            f"FROM {_NODE_STATES_TABLE} "
            f"WHERE {_COL_NS_GRAPH_INSTANCE_ID} = ? AND {_COL_NS_NODE_NAME} = ? "
            f"AND {_COL_NS_INVOCATION_ID} = ? "
            f"ORDER BY {_COL_NS_VERSION} DESC LIMIT 1",
            (graph_instance_id, node_name, invocation_id),
        ).fetchone()
        return self._row_to_record(row) if row is not None else None

    def load_latest(self, graph_instance_id: int, node_name: str) -> NodeInvocationRecord | None:
        row = self._conn.execute(
            f"SELECT {_COL_NODE_STATE_ID}, {_COL_NS_GRAPH_INSTANCE_ID}, "
            f"{_COL_NS_NODE_NAME}, {_COL_NS_VERSION}, {_COL_NS_PARENT_VERSION}, "
            f"{_COL_NS_STATUS}, {_COL_NS_INVOCATION_ID}, {_COL_NS_STATE_JSON}, "
            f"{_COL_NS_SUSPENDED}, {_COL_NS_CREATED_AT}, {_COL_NS_UPDATED_AT} "
            f"FROM {_NODE_STATES_TABLE} "
            f"WHERE {_COL_NS_GRAPH_INSTANCE_ID} = ? AND {_COL_NS_NODE_NAME} = ? "
            f"ORDER BY {_COL_NS_VERSION} DESC LIMIT 1",
            (graph_instance_id, node_name),
        ).fetchone()
        return self._row_to_record(row) if row is not None else None

    def load_latest_completed(
        self, graph_instance_id: int, node_name: str
    ) -> NodeInvocationRecord | None:
        row = self._conn.execute(
            f"SELECT {_COL_NODE_STATE_ID}, {_COL_NS_GRAPH_INSTANCE_ID}, "
            f"{_COL_NS_NODE_NAME}, {_COL_NS_VERSION}, {_COL_NS_PARENT_VERSION}, "
            f"{_COL_NS_STATUS}, {_COL_NS_INVOCATION_ID}, {_COL_NS_STATE_JSON}, "
            f"{_COL_NS_SUSPENDED}, {_COL_NS_CREATED_AT}, {_COL_NS_UPDATED_AT} "
            f"FROM {_NODE_STATES_TABLE} "
            f"WHERE {_COL_NS_GRAPH_INSTANCE_ID} = ? AND {_COL_NS_NODE_NAME} = ? "
            f"AND {_COL_NS_STATUS} = ? "
            f"ORDER BY {_COL_NS_VERSION} DESC LIMIT 1",
            (graph_instance_id, node_name, InvocationStatus.COMPLETED.value),
        ).fetchone()
        return self._row_to_record(row) if row is not None else None

    def query_versions(
        self,
        graph_instance_id: int,
        node_name: str,
        status_filter: set[InvocationStatus] | None = None,
    ) -> list[NodeInvocationRecord]:
        if status_filter is None:
            rows = self._conn.execute(
                f"SELECT {_COL_NODE_STATE_ID}, {_COL_NS_GRAPH_INSTANCE_ID}, "
                f"{_COL_NS_NODE_NAME}, {_COL_NS_VERSION}, {_COL_NS_PARENT_VERSION}, "
                f"{_COL_NS_STATUS}, {_COL_NS_INVOCATION_ID}, {_COL_NS_STATE_JSON}, "
                f"{_COL_NS_SUSPENDED}, {_COL_NS_CREATED_AT}, {_COL_NS_UPDATED_AT} "
                f"FROM {_NODE_STATES_TABLE} "
                f"WHERE {_COL_NS_GRAPH_INSTANCE_ID} = ? AND {_COL_NS_NODE_NAME} = ? "
                f"ORDER BY {_COL_NS_VERSION} DESC",
                (graph_instance_id, node_name),
            ).fetchall()
        else:
            placeholders = ",".join("?" for _ in status_filter)
            rows = self._conn.execute(
                f"SELECT {_COL_NODE_STATE_ID}, {_COL_NS_GRAPH_INSTANCE_ID}, "
                f"{_COL_NS_NODE_NAME}, {_COL_NS_VERSION}, {_COL_NS_PARENT_VERSION}, "
                f"{_COL_NS_STATUS}, {_COL_NS_INVOCATION_ID}, {_COL_NS_STATE_JSON}, "
                f"{_COL_NS_SUSPENDED}, {_COL_NS_CREATED_AT}, {_COL_NS_UPDATED_AT} "
                f"FROM {_NODE_STATES_TABLE} "
                f"WHERE {_COL_NS_GRAPH_INSTANCE_ID} = ? AND {_COL_NS_NODE_NAME} = ? "
                f"AND {_COL_NS_STATUS} IN ({placeholders}) "
                f"ORDER BY {_COL_NS_VERSION} DESC",
                (graph_instance_id, node_name, *[s.value for s in status_filter]),
            ).fetchall()
        return [self._row_to_record(row) for row in rows]

    @staticmethod
    def _row_to_record(row: tuple[Any, ...]) -> NodeInvocationRecord:
        (
            _node_state_id,
            graph_instance_id,
            node_name,
            version,
            parent_version,
            status,
            invocation_id,
            state_json,
            suspended,
            created_at,
            updated_at,
        ) = row
        return NodeInvocationRecord(
            invocation_id=invocation_id,
            graph_instance_id=graph_instance_id,
            node_name=node_name,
            version=version,
            parent_version=parent_version,
            status=InvocationStatus(status),
            state_json=json.loads(state_json),
            suspended=bool(suspended),
            created_at=created_at,
            updated_at=updated_at,
        )


class NodeStateFactory(ABC):
    """Create the default NodeState persistence strategy."""

    @abstractmethod
    def create(self) -> NodeState: ...


class NullNodeStateFactory(NodeStateFactory):
    """Factory for `NullNodeState` — no-op strategy."""

    def create(self) -> NullNodeState:
        return NullNodeState()


class SimpleNodeStateFactory(NodeStateFactory):
    """Factory for `SimpleNodeState` — in-memory dict strategy."""

    def create(self) -> SimpleNodeState:
        return SimpleNodeState()


class SqliteNodeStateFactory(NodeStateFactory):
    """Factory for `SqliteNodeState` — accepts a shared connection.

    The connection is owned by the caller; the factory does NOT close it.
    Multiple factories (NodeState + GraphMetadata + Deliver) sharing one
    connection is the supported pattern for per-workspace SQLite files.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._conn = connection

    def create(self) -> SqliteNodeState:
        return SqliteNodeState(self._conn)


__all__ = [
    "NodeInvocationRecord",
    "NodeState",
    "NodeStateFactory",
    "NullNodeState",
    "NullNodeStateFactory",
    "SimpleNodeState",
    "SimpleNodeStateFactory",
    "SqliteNodeState",
    "SqliteNodeStateFactory",
]
