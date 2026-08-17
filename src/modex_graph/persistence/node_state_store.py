# ruff: noqa: ANN401

"""``NodeStateStore`` — lifecycle + version chain + CAS authority for node invocations.

The store is scoped to ONE ``graph_instance_id`` (captured at construction).
All methods take ``node_id`` only — the ``graph_instance_id`` is implicit.

Lifecycle (rule 15: single authority — no parallel ``NodeState`` path):

- ``begin_invocation`` — INSERT a new ``RUNNING`` record (version = max + 1,
  parent_version from ``load_latest_completed``). If a prior ``RUNNING``
  record exists, it is marked ``CRASHED`` (orphan cleanup).
- ``complete_invocation`` / ``cancel_invocation`` — STRICT CAS:
  ``UPDATE ... WHERE status='running'``. If rowcount=0, raise
  ``InvocationStateError``.
- ``crash_invocation`` / ``finalize_invocation`` — TOLERANT: idempotent
  no-op if already terminal. ``crash`` updates ``RUNNING`` → ``CRASHED``.
  ``finalize`` promotes orphan ``RUNNING`` → ``CRASHED`` and leaves terminal
  records untouched.

Terminal states: ``COMPLETED``, ``CANCELED``, ``CRASHED`` — no transition
FROM terminal.

SQL schema (``node_states`` table):

    node_state_id     BIGINT PRIMARY KEY,
    graph_instance_id BIGINT NOT NULL,
    node_id           TEXT NOT NULL,
    version           INTEGER NOT NULL,
    parent_version    INTEGER,
    invocation_id     BIGINT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'running'
                      CHECK (status IN ('running','completed','canceled','crashed')),
    created_at        INTEGER NOT NULL,
    updated_at        INTEGER NOT NULL,
    UNIQUE (graph_instance_id, node_id, version)

Implementations:

- ``NullNodeStateStore`` — no-op; ``begin_invocation`` returns a valid
  ``InvocationContext`` (generated invocation_id, version=0,
  parent_version=None). Used by ``create_null_coordinator``.
- ``InMemoryNodeStateStore`` — dict-backed, default for tests.
- ``SqliteNodeStateStore`` — SQLite adapter with CAS via
  ``UPDATE ... WHERE status='running'``.
"""

from __future__ import annotations

import sqlite3
from abc import ABC, abstractmethod
from typing import Any

from ..constants import InvocationStatus
from ..exceptions import InvocationStateError
from ..id_generator import default_id_generator
from ._time import now_ms
from .graph_metadata import InvocationContext, NodeInvocationRecord

# ── Table / column name constants ─────────────────────────────────────────
_NODE_STATE_TABLE = "node_states"
_COL_NODE_STATE_ID = "node_state_id"
_COL_GRAPH_INSTANCE_ID = "graph_instance_id"
_COL_NODE_ID = "node_id"
_COL_VERSION = "version"
_COL_PARENT_VERSION = "parent_version"
_COL_STATUS = "status"
_COL_INVOCATION_ID = "invocation_id"
_COL_CREATED_AT = "created_at"
_COL_UPDATED_AT = "updated_at"

# Columns selected in every query, in order expected by _row_to_record.
_SELECT_COLUMNS = (
    f"{_COL_NODE_STATE_ID}, {_COL_GRAPH_INSTANCE_ID}, "
    f"{_COL_NODE_ID}, {_COL_VERSION}, {_COL_PARENT_VERSION}, "
    f"{_COL_STATUS}, {_COL_INVOCATION_ID}, "
    f"{_COL_CREATED_AT}, {_COL_UPDATED_AT}"
)


class NodeStateStore(ABC):
    """Lifecycle + version chain + CAS authority for node invocations.

    Scoped to ONE ``graph_instance_id`` (captured at construction). All
    methods take ``node_id`` only.

    Rule 15: this is the SINGLE lifecycle authority — no parallel
    ``NodeState`` path. ``Node.run()`` calls these methods directly via
    ``ctx.node_state_store``.

    All methods are synchronous and must be called from the event-loop
    thread only. The caller owns the ``sqlite3.Connection`` and manages its
    lifetime — the store never closes it.
    """

    def __init__(self, graph_instance_id: int) -> None:
        self._graph_instance_id = graph_instance_id

    @property
    def graph_instance_id(self) -> int:
        return self._graph_instance_id

    # ── Lifecycle ───────────────────────────────────────────────────────

    @abstractmethod
    def begin_invocation(self, node_id: str) -> InvocationContext:
        """Begin a new invocation. INSERT a new RUNNING record.

        If a prior RUNNING record exists, mark it CRASHED (orphan cleanup).
        Records begin directly as RUNNING.

        Returns the ``InvocationContext`` with invocation_id, version,
        and parent_version.
        """
        ...

    @abstractmethod
    def complete_invocation(self, invocation: InvocationContext) -> None:
        """Mark an invocation COMPLETED. STRICT CAS — raises on lost race."""
        ...

    @abstractmethod
    def crash_invocation(self, invocation: InvocationContext) -> None:
        """Mark an invocation CRASHED. TOLERANT — no-op if already terminal."""
        ...

    @abstractmethod
    def cancel_invocation(self, invocation: InvocationContext) -> None:
        """Mark an invocation CANCELED. STRICT CAS — raises on lost race."""
        ...

    @abstractmethod
    def finalize_invocation(self, invocation: InvocationContext) -> None:
        """Safety net: orphan RUNNING → CRASHED. TOLERANT."""
        ...

    # ── Query ───────────────────────────────────────────────────────────

    @abstractmethod
    def load_latest(self, node_id: str) -> NodeInvocationRecord | None:
        """Load the latest record for a node (highest version)."""
        ...

    @abstractmethod
    def load_latest_completed(self, node_id: str) -> NodeInvocationRecord | None:
        """Load the latest COMPLETED record for a node."""
        ...

    @abstractmethod
    def load_by_invocation_id(
        self, node_id: str, invocation_id: int
    ) -> NodeInvocationRecord | None:
        """Load a record by its ``invocation_id`` within a node's version chain.

        Returns ``None`` if no record for ``node_id`` has the given
        ``invocation_id``. Used by recovery to check whether a specific
        consuming invocation is COMPLETED (auto-promote delivers).
        """
        ...

    @abstractmethod
    def query_versions(
        self,
        node_id: str,
        status_filter: set[InvocationStatus] | None = None,
    ) -> list[NodeInvocationRecord]:
        """Query versions for a node, optionally filtered by status."""
        ...

    @abstractmethod
    def list_nodes(self) -> list[str]:
        """List all node IDs that have state snapshots."""
        ...

    @abstractmethod
    def query_all(self, status_filter: set[InvocationStatus]) -> list[NodeInvocationRecord]:
        """Query across ALL nodes, filtered by status."""
        ...

    @abstractmethod
    def clear(self) -> None:
        """Delete ALL node states for this graph instance."""
        ...


# ── Null ─────────────────────────────────────────────────────────────────


class NullNodeStateStore(NodeStateStore):
    """No-op ``NodeStateStore`` — no persistence.

    ``begin_invocation`` returns a valid ``InvocationContext`` (generated
    invocation_id, version=0, parent_version=None). All other lifecycle
    methods are no-ops. All queries return None / empty. Used by
    ``create_null_coordinator``.
    """

    def begin_invocation(self, node_id: str) -> InvocationContext:
        invocation_id = default_id_generator().generate()
        return InvocationContext(
            invocation_id=invocation_id,
            node_id=node_id,
            version=0,
            parent_version=None,
        )

    def complete_invocation(self, invocation: InvocationContext) -> None:
        pass

    def crash_invocation(self, invocation: InvocationContext) -> None:
        pass

    def cancel_invocation(self, invocation: InvocationContext) -> None:
        pass

    def finalize_invocation(self, invocation: InvocationContext) -> None:
        pass

    def load_latest(self, node_id: str) -> NodeInvocationRecord | None:
        return None

    def load_latest_completed(self, node_id: str) -> NodeInvocationRecord | None:
        return None

    def load_by_invocation_id(
        self, node_id: str, invocation_id: int
    ) -> NodeInvocationRecord | None:
        return None

    def query_versions(
        self,
        node_id: str,
        status_filter: set[InvocationStatus] | None = None,
    ) -> list[NodeInvocationRecord]:
        return []

    def list_nodes(self) -> list[str]:
        return []

    def query_all(self, status_filter: set[InvocationStatus]) -> list[NodeInvocationRecord]:
        return []

    def clear(self) -> None:
        pass


# ── InMemory ─────────────────────────────────────────────────────────────


class InMemoryNodeStateStore(NodeStateStore):
    """Dict-backed ``NodeStateStore``. Default for tests + single-process runs.

    Stores records in a dict keyed by ``node_id``. Each node has a list
    of ``NodeInvocationRecord`` ordered by version ascending. Lifecycle
    methods mutate records in place (CAS via status check).
    """

    def __init__(self, graph_instance_id: int) -> None:
        super().__init__(graph_instance_id)
        self._records: dict[str, list[NodeInvocationRecord]] = {}

    def begin_invocation(self, node_id: str) -> InvocationContext:
        gid = self._graph_instance_id
        records = self._records.get(node_id, [])

        if records:
            latest = records[-1]
            if latest.status == InvocationStatus.RUNNING:
                ts = now_ms()
                crashed = latest.model_copy(
                    update={"status": InvocationStatus.CRASHED, "updated_at": ts}
                )
                records[-1] = crashed
                self._records[node_id] = records

        # version = max(all existing versions) + 1.
        version = max((r.version for r in records), default=-1) + 1

        # parent_version from load_latest_completed.
        completed = [r for r in records if r.status == InvocationStatus.COMPLETED]
        parent_version = max(completed, key=lambda r: r.version).version if completed else None

        invocation_id = default_id_generator().generate()
        ts = now_ms()
        record = NodeInvocationRecord(
            invocation_id=invocation_id,
            graph_instance_id=gid,
            node_id=node_id,
            version=version,
            parent_version=parent_version,
            status=InvocationStatus.RUNNING,
            created_at=ts,
            updated_at=ts,
        )
        records.append(record)
        self._records[node_id] = records

        return InvocationContext(
            invocation_id=invocation_id,
            node_id=node_id,
            version=version,
            parent_version=parent_version,
        )

    def _find_current(self, invocation: InvocationContext) -> NodeInvocationRecord | None:
        records = self._records.get(invocation.node_id, [])
        for r in records:
            if r.version == invocation.version:
                return r
        return None

    def complete_invocation(self, invocation: InvocationContext) -> None:
        current = self._find_current(invocation)
        if current is None:
            raise InvocationStateError(
                f"No record found for {invocation.node_id!r} version {invocation.version}."
            )
        if current.status != InvocationStatus.RUNNING:
            raise InvocationStateError(
                f"CAS failed: {invocation.node_id!r} v{invocation.version} "
                f"is {current.status.value}, expected RUNNING."
            )
        ts = now_ms()
        updated = current.model_copy(
            update={
                "status": InvocationStatus.COMPLETED,
                "updated_at": ts,
            }
        )
        records = self._records[invocation.node_id]
        idx = records.index(current)
        records[idx] = updated

    def cancel_invocation(self, invocation: InvocationContext) -> None:
        current = self._find_current(invocation)
        if current is None:
            raise InvocationStateError(
                f"No record found for {invocation.node_id!r} version {invocation.version}."
            )
        if current.status != InvocationStatus.RUNNING:
            raise InvocationStateError(
                f"CAS failed: {invocation.node_id!r} v{invocation.version} "
                f"is {current.status.value}, expected RUNNING."
            )
        ts = now_ms()
        updated = current.model_copy(
            update={
                "status": InvocationStatus.CANCELED,
                "updated_at": ts,
            }
        )
        records = self._records[invocation.node_id]
        idx = records.index(current)
        records[idx] = updated

    def crash_invocation(self, invocation: InvocationContext) -> None:
        current = self._find_current(invocation)
        if current is None:
            return
        # TOLERANT: no-op if already terminal.
        if current.status != InvocationStatus.RUNNING:
            return
        ts = now_ms()
        updated = current.model_copy(
            update={
                "status": InvocationStatus.CRASHED,
                "updated_at": ts,
            }
        )
        records = self._records[invocation.node_id]
        idx = records.index(current)
        records[idx] = updated

    def finalize_invocation(self, invocation: InvocationContext) -> None:
        current = self._find_current(invocation)
        if current is None:
            return
        # Terminal — don't touch.
        if current.status in (
            InvocationStatus.COMPLETED,
            InvocationStatus.CANCELED,
            InvocationStatus.CRASHED,
        ):
            return
        ts = now_ms()
        updated = current.model_copy(
            update={
                "status": InvocationStatus.CRASHED,
                "updated_at": ts,
            }
        )
        records = self._records[invocation.node_id]
        idx = records.index(current)
        records[idx] = updated

    def load_latest(self, node_id: str) -> NodeInvocationRecord | None:
        records = self._records.get(node_id, [])
        if not records:
            return None
        return records[-1]

    def load_latest_completed(self, node_id: str) -> NodeInvocationRecord | None:
        records = self._records.get(node_id, [])
        completed = [r for r in records if r.status == InvocationStatus.COMPLETED]
        if not completed:
            return None
        return max(completed, key=lambda r: r.version)

    def load_by_invocation_id(
        self, node_id: str, invocation_id: int
    ) -> NodeInvocationRecord | None:
        records = self._records.get(node_id, [])
        for r in records:
            if r.invocation_id == invocation_id:
                return r
        return None

    def query_versions(
        self,
        node_id: str,
        status_filter: set[InvocationStatus] | None = None,
    ) -> list[NodeInvocationRecord]:
        records = self._records.get(node_id, [])
        filtered = (
            [r for r in records if r.status in status_filter]
            if status_filter is not None
            else list(records)
        )
        return sorted(filtered, key=lambda r: r.version, reverse=True)

    def list_nodes(self) -> list[str]:
        return sorted(self._records.keys())

    def query_all(self, status_filter: set[InvocationStatus]) -> list[NodeInvocationRecord]:
        result: list[NodeInvocationRecord] = []
        for records in self._records.values():
            result.extend(r for r in records if r.status in status_filter)
        return sorted(result, key=lambda r: (r.node_id, r.version), reverse=True)

    def clear(self) -> None:
        self._records.clear()


# ── SQLite ───────────────────────────────────────────────────────────────


class SqliteNodeStateStore(NodeStateStore):
    """SQLite-backed ``NodeStateStore`` with CAS semantics.

    Uses ``UPDATE ... WHERE status='running'`` for strict transitions
    (complete / cancel). ``rowcount == 0`` indicates a lost race →
    ``InvocationStateError``.

    The store uses a single caller-owned ``sqlite3.Connection`` for its
    lifetime. The caller creates the connection (with ``check_same_thread``
    set as needed) and passes it to all stores sharing one workspace DB;
    the store never closes it.
    """

    def __init__(self, connection: sqlite3.Connection, graph_instance_id: int) -> None:
        super().__init__(graph_instance_id)
        self._conn = connection
        self._init_schema()

    def _init_schema(self) -> None:
        """Create the `node_states` table + indexes if they don't exist.

        Detects old-schema tables (missing ``node_id`` or containing retired
        ``state_json`` / ``suspended`` columns) and rebuilds them because
        SQLite ``ALTER TABLE`` cannot remove columns on all supported builds.
        Valid lifecycle rows are preserved when the old table has the full
        invocation identity schema.

        The DDL matches `001_initial.sql` table 18 (including the `status`
        CHECK constraint and the ``UNIQUE (graph_instance_id, node_id,
        version)`` constraint).
        """
        conn = self._conn
        existing = {
            row[1] for row in conn.execute(f"PRAGMA table_info({_NODE_STATE_TABLE})").fetchall()
        }
        legacy_table: str | None = None
        if existing:
            if _COL_NODE_ID not in existing:
                conn.execute(f"DROP TABLE IF EXISTS {_NODE_STATE_TABLE}")
            elif {"state_json", "suspended"} & existing:
                migratable_columns = {
                    _COL_NODE_STATE_ID,
                    _COL_GRAPH_INSTANCE_ID,
                    _COL_NODE_ID,
                    _COL_VERSION,
                    _COL_PARENT_VERSION,
                    _COL_STATUS,
                    _COL_INVOCATION_ID,
                    _COL_CREATED_AT,
                    _COL_UPDATED_AT,
                }
                if migratable_columns <= existing:
                    legacy_table = f"{_NODE_STATE_TABLE}_legacy"
                    conn.execute(f"ALTER TABLE {_NODE_STATE_TABLE} RENAME TO {legacy_table}")
                else:
                    conn.execute(f"DROP TABLE {_NODE_STATE_TABLE}")
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {_NODE_STATE_TABLE} ("
            f"{_COL_NODE_STATE_ID} INTEGER PRIMARY KEY, "
            f"{_COL_GRAPH_INSTANCE_ID} INTEGER NOT NULL, "
            f"{_COL_NODE_ID} TEXT NOT NULL, "
            f"{_COL_VERSION} INTEGER NOT NULL DEFAULT 0, "
            f"{_COL_PARENT_VERSION} INTEGER, "
            f"{_COL_STATUS} TEXT NOT NULL DEFAULT '{InvocationStatus.RUNNING.value}' "
            f"CHECK ({_COL_STATUS} IN ("
            f"'{InvocationStatus.RUNNING.value}', "
            f"'{InvocationStatus.COMPLETED.value}', "
            f"'{InvocationStatus.CANCELED.value}', "
            f"'{InvocationStatus.CRASHED.value}'"
            f")), "
            f"{_COL_INVOCATION_ID} BIGINT NOT NULL DEFAULT 0, "
            f"{_COL_CREATED_AT} INTEGER NOT NULL, "
            f"{_COL_UPDATED_AT} INTEGER NOT NULL, "
            f"UNIQUE ({_COL_GRAPH_INSTANCE_ID}, {_COL_NODE_ID}, {_COL_VERSION})"
            f")"
        )
        if legacy_table is not None:
            columns = ", ".join(
                (
                    _COL_NODE_STATE_ID,
                    _COL_GRAPH_INSTANCE_ID,
                    _COL_NODE_ID,
                    _COL_VERSION,
                    _COL_PARENT_VERSION,
                    _COL_STATUS,
                    _COL_INVOCATION_ID,
                    _COL_CREATED_AT,
                    _COL_UPDATED_AT,
                )
            )
            conn.execute(
                f"INSERT INTO {_NODE_STATE_TABLE} ({columns}) "
                f"SELECT {columns} FROM {legacy_table}"
            )
            conn.execute(f"DROP TABLE {legacy_table}")
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{_NODE_STATE_TABLE}_latest "
            f"ON {_NODE_STATE_TABLE} "
            f"({_COL_GRAPH_INSTANCE_ID}, {_COL_NODE_ID}, {_COL_VERSION} DESC)"
        )
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{_NODE_STATE_TABLE}_node "
            f"ON {_NODE_STATE_TABLE} ({_COL_GRAPH_INSTANCE_ID}, {_COL_NODE_ID})"
        )
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{_NODE_STATE_TABLE}_status "
            f"ON {_NODE_STATE_TABLE} ({_COL_GRAPH_INSTANCE_ID}, {_COL_NODE_ID}, {_COL_STATUS})"
        )
        conn.commit()

    # ── Lifecycle ───────────────────────────────────────────────────────

    def begin_invocation(self, node_id: str) -> InvocationContext:
        gid = self._graph_instance_id
        conn = self._conn

        latest = self.load_latest(node_id)
        if latest is not None and latest.status == InvocationStatus.RUNNING:
            ts = now_ms()
            conn.execute(
                f"UPDATE {_NODE_STATE_TABLE} "
                f"SET {_COL_STATUS} = ?, {_COL_UPDATED_AT} = ? "
                f"WHERE {_COL_GRAPH_INSTANCE_ID} = ? AND {_COL_NODE_ID} = ? "
                f"AND {_COL_VERSION} = ? AND {_COL_STATUS} = ?",
                (
                    InvocationStatus.CRASHED.value,
                    ts,
                    gid, node_id, latest.version,
                    InvocationStatus.RUNNING.value,
                ),
            )
            conn.commit()

        # version = max(all existing versions) + 1.
        row = conn.execute(
            f"SELECT MAX({_COL_VERSION}) FROM {_NODE_STATE_TABLE} "
            f"WHERE {_COL_GRAPH_INSTANCE_ID} = ? AND {_COL_NODE_ID} = ?",
            (gid, node_id),
        ).fetchone()
        version = (row[0] + 1) if row[0] is not None else 0

        # parent_version from load_latest_completed.
        latest_completed = self.load_latest_completed(node_id)
        parent_version = latest_completed.version if latest_completed is not None else None

        invocation_id = default_id_generator().generate()
        node_state_id = default_id_generator().generate()
        ts = now_ms()
        conn.execute(
            f"INSERT INTO {_NODE_STATE_TABLE} "
            f"({_COL_NODE_STATE_ID}, {_COL_GRAPH_INSTANCE_ID}, {_COL_NODE_ID}, "
            f"{_COL_VERSION}, {_COL_PARENT_VERSION}, {_COL_STATUS}, "
            f"{_COL_INVOCATION_ID}, {_COL_CREATED_AT}, {_COL_UPDATED_AT}) "
            f"VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                node_state_id,
                gid,
                node_id,
                version,
                parent_version,
                InvocationStatus.RUNNING.value,
                invocation_id,
                ts,
                ts,
            ),
        )
        conn.commit()

        return InvocationContext(
            invocation_id=invocation_id,
            node_id=node_id,
            version=version,
            parent_version=parent_version,
        )

    def complete_invocation(self, invocation: InvocationContext) -> None:
        self._strict_update(invocation, status=InvocationStatus.COMPLETED)

    def cancel_invocation(self, invocation: InvocationContext) -> None:
        self._strict_update(invocation, status=InvocationStatus.CANCELED)

    def _strict_update(
        self,
        invocation: InvocationContext,
        *,
        status: InvocationStatus,
    ) -> None:
        """STRICT CAS: update only if status is RUNNING.

        Raises ``InvocationStateError`` if rowcount=0 (lost race or
        already terminal).
        """
        gid = self._graph_instance_id
        conn = self._conn
        ts = now_ms()

        cursor = conn.execute(
            f"UPDATE {_NODE_STATE_TABLE} "
            f"SET {_COL_STATUS} = ?, {_COL_UPDATED_AT} = ? "
            f"WHERE {_COL_GRAPH_INSTANCE_ID} = ? AND {_COL_NODE_ID} = ? "
            f"AND {_COL_VERSION} = ? AND {_COL_STATUS} = ?",
            (
                status.value,
                ts,
                gid,
                invocation.node_id,
                invocation.version,
                InvocationStatus.RUNNING.value,
            ),
        )
        conn.commit()

        if cursor.rowcount == 0:
            current = self.load_latest(invocation.node_id)
            cur_status = current.status.value if current else "missing"
            raise InvocationStateError(
                f"CAS failed: {invocation.node_id!r} v{invocation.version} "
                f"is {cur_status}, expected RUNNING."
            )

    def crash_invocation(self, invocation: InvocationContext) -> None:
        """TOLERANT: update RUNNING → CRASHED. No-op if already terminal."""
        gid = self._graph_instance_id
        conn = self._conn
        ts = now_ms()
        conn.execute(
            f"UPDATE {_NODE_STATE_TABLE} "
            f"SET {_COL_STATUS} = ?, {_COL_UPDATED_AT} = ? "
            f"WHERE {_COL_GRAPH_INSTANCE_ID} = ? AND {_COL_NODE_ID} = ? "
            f"AND {_COL_VERSION} = ? AND {_COL_STATUS} = ?",
            (
                InvocationStatus.CRASHED.value,
                ts,
                gid,
                invocation.node_id,
                invocation.version,
                InvocationStatus.RUNNING.value,
            ),
        )
        conn.commit()

    def finalize_invocation(self, invocation: InvocationContext) -> None:
        """TOLERANT: orphan RUNNING → CRASHED; terminal records are unchanged."""
        gid = self._graph_instance_id
        conn = self._conn
        ts = now_ms()
        conn.execute(
            f"UPDATE {_NODE_STATE_TABLE} "
            f"SET {_COL_STATUS} = ?, {_COL_UPDATED_AT} = ? "
            f"WHERE {_COL_GRAPH_INSTANCE_ID} = ? AND {_COL_NODE_ID} = ? "
            f"AND {_COL_VERSION} = ? AND {_COL_STATUS} = ?",
            (
                InvocationStatus.CRASHED.value,
                ts,
                gid,
                invocation.node_id,
                invocation.version,
                InvocationStatus.RUNNING.value,
            ),
        )
        conn.commit()

    # ── Query ───────────────────────────────────────────────────────────

    def load_latest(self, node_id: str) -> NodeInvocationRecord | None:
        gid = self._graph_instance_id
        row = self._conn.execute(
            f"SELECT {_SELECT_COLUMNS} FROM {_NODE_STATE_TABLE} "
            f"WHERE {_COL_GRAPH_INSTANCE_ID} = ? AND {_COL_NODE_ID} = ? "
            f"ORDER BY {_COL_VERSION} DESC LIMIT 1",
            (gid, node_id),
        ).fetchone()
        return self._row_to_record(row) if row is not None else None

    def load_latest_completed(self, node_id: str) -> NodeInvocationRecord | None:
        gid = self._graph_instance_id
        row = self._conn.execute(
            f"SELECT {_SELECT_COLUMNS} FROM {_NODE_STATE_TABLE} "
            f"WHERE {_COL_GRAPH_INSTANCE_ID} = ? AND {_COL_NODE_ID} = ? "
            f"AND {_COL_STATUS} = ? "
            f"ORDER BY {_COL_VERSION} DESC LIMIT 1",
            (gid, node_id, InvocationStatus.COMPLETED.value),
        ).fetchone()
        return self._row_to_record(row) if row is not None else None

    def load_by_invocation_id(
        self, node_id: str, invocation_id: int
    ) -> NodeInvocationRecord | None:
        gid = self._graph_instance_id
        row = self._conn.execute(
            f"SELECT {_SELECT_COLUMNS} FROM {_NODE_STATE_TABLE} "
            f"WHERE {_COL_GRAPH_INSTANCE_ID} = ? AND {_COL_NODE_ID} = ? "
            f"AND {_COL_INVOCATION_ID} = ? "
            f"ORDER BY {_COL_VERSION} DESC LIMIT 1",
            (gid, node_id, invocation_id),
        ).fetchone()
        return self._row_to_record(row) if row is not None else None

    def query_versions(
        self,
        node_id: str,
        status_filter: set[InvocationStatus] | None = None,
    ) -> list[NodeInvocationRecord]:
        gid = self._graph_instance_id
        if status_filter is None:
            rows = self._conn.execute(
                f"SELECT {_SELECT_COLUMNS} FROM {_NODE_STATE_TABLE} "
                f"WHERE {_COL_GRAPH_INSTANCE_ID} = ? AND {_COL_NODE_ID} = ? "
                f"ORDER BY {_COL_VERSION} DESC",
                (gid, node_id),
            ).fetchall()
        else:
            placeholders = ",".join("?" for _ in status_filter)
            rows = self._conn.execute(
                f"SELECT {_SELECT_COLUMNS} FROM {_NODE_STATE_TABLE} "
                f"WHERE {_COL_GRAPH_INSTANCE_ID} = ? AND {_COL_NODE_ID} = ? "
                f"AND {_COL_STATUS} IN ({placeholders}) "
                f"ORDER BY {_COL_VERSION} DESC",
                (gid, node_id, *[s.value for s in status_filter]),
            ).fetchall()
        return [self._row_to_record(row) for row in rows]

    def list_nodes(self) -> list[str]:
        gid = self._graph_instance_id
        rows = self._conn.execute(
            f"SELECT DISTINCT {_COL_NODE_ID} FROM {_NODE_STATE_TABLE} "
            f"WHERE {_COL_GRAPH_INSTANCE_ID} = ? "
            f"ORDER BY {_COL_NODE_ID}",
            (gid,),
        ).fetchall()
        return [r[0] for r in rows]

    def query_all(self, status_filter: set[InvocationStatus]) -> list[NodeInvocationRecord]:
        gid = self._graph_instance_id
        placeholders = ",".join("?" for _ in status_filter)
        rows = self._conn.execute(
            f"SELECT {_SELECT_COLUMNS} FROM {_NODE_STATE_TABLE} "
            f"WHERE {_COL_GRAPH_INSTANCE_ID} = ? "
            f"AND {_COL_STATUS} IN ({placeholders}) "
            f"ORDER BY {_COL_NODE_ID}, {_COL_VERSION} DESC",
            (gid, *[s.value for s in status_filter]),
        ).fetchall()
        return [self._row_to_record(row) for row in rows]

    def clear(self) -> None:
        gid = self._graph_instance_id
        self._conn.execute(
            f"DELETE FROM {_NODE_STATE_TABLE} WHERE {_COL_GRAPH_INSTANCE_ID} = ?",
            (gid,),
        )
        self._conn.commit()

    @staticmethod
    def _row_to_record(row: tuple[Any, ...]) -> NodeInvocationRecord:
        (
            _node_state_id,
            graph_instance_id,
            node_id,
            version,
            parent_version,
            status,
            invocation_id,
            created_at,
            updated_at,
        ) = row
        return NodeInvocationRecord(
            invocation_id=invocation_id,
            graph_instance_id=graph_instance_id,
            node_id=node_id,
            version=version,
            parent_version=parent_version,
            status=InvocationStatus(status),
            created_at=created_at,
            updated_at=updated_at,
        )


__all__ = [
    "NodeStateStore",
    "NullNodeStateStore",
    "InMemoryNodeStateStore",
    "SqliteNodeStateStore",
]
