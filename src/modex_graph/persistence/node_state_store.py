# ruff: noqa: ANN401

"""``NodeStateStore`` — lifecycle + version chain + CAS authority for node invocations.

The store is scoped to ONE ``graph_instance_id`` (captured at construction).
All methods take ``node_name`` only — the ``graph_instance_id`` is implicit.

Lifecycle (rule 15: single authority — no parallel ``NodeState`` path):

- ``begin_invocation`` — INSERT a new ``RUNNING`` record (version = max + 1,
  parent_version from ``load_latest_completed``). If a prior non-suspended
  ``RUNNING`` record exists, it is marked ``CRASHED`` (orphan cleanup). A
  prior suspended ``RUNNING`` is left in place (valid rebuild source).
- ``complete_invocation`` / ``suspend_invocation`` / ``cancel_invocation`` —
  STRICT CAS: ``UPDATE ... WHERE status='running' AND suspended=0``. If
  rowcount=0, raise ``InvocationStateError``.
- ``crash_invocation`` / ``finalize_invocation`` — TOLERANT: idempotent
  no-op if already terminal. ``crash`` updates ``RUNNING`` → ``CRASHED``.
  ``finalize`` promotes orphan non-suspended ``RUNNING`` → ``CRASHED``;
  leaves terminal and suspended records untouched.

Terminal states: ``COMPLETED``, ``CANCELED``, ``CRASHED`` — no transition
FROM terminal.

SQL schema (``node_states`` table):

    node_state_id     BIGINT PRIMARY KEY,
    graph_instance_id BIGINT NOT NULL,
    node_name         TEXT NOT NULL,
    version           INTEGER NOT NULL,
    parent_version    INTEGER,
    invocation_id     BIGINT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'running'
                      CHECK (status IN ('running','completed','canceled','crashed')),
    state_json        TEXT NOT NULL,
    suspended         INTEGER NOT NULL DEFAULT 0,
    created_at        INTEGER NOT NULL,
    updated_at        INTEGER NOT NULL,
    UNIQUE (graph_instance_id, node_name, version)

Implementations:

- ``NullNodeStateStore`` — no-op; ``begin_invocation`` returns a valid
  ``InvocationContext`` (generated invocation_id, version=0,
  parent_version=None). Used by ``create_null_coordinator``.
- ``InMemoryNodeStateStore`` — dict-backed, default for tests.
- ``SqliteNodeStateStore`` — SQLite adapter with CAS via
  ``UPDATE ... WHERE status='running'``.
"""

from __future__ import annotations

import json
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
_COL_NODE_NAME = "node_name"
_COL_VERSION = "version"
_COL_PARENT_VERSION = "parent_version"
_COL_STATUS = "status"
_COL_INVOCATION_ID = "invocation_id"
_COL_STATE_JSON = "state_json"
_COL_SUSPENDED = "suspended"
_COL_CREATED_AT = "created_at"
_COL_UPDATED_AT = "updated_at"

# Columns selected in every query, in order expected by _row_to_record.
_SELECT_COLUMNS = (
    f"{_COL_NODE_STATE_ID}, {_COL_GRAPH_INSTANCE_ID}, "
    f"{_COL_NODE_NAME}, {_COL_VERSION}, {_COL_PARENT_VERSION}, "
    f"{_COL_STATUS}, {_COL_INVOCATION_ID}, {_COL_STATE_JSON}, "
    f"{_COL_SUSPENDED}, {_COL_CREATED_AT}, {_COL_UPDATED_AT}"
)


class NodeStateStore(ABC):
    """Lifecycle + version chain + CAS authority for node invocations.

    Scoped to ONE ``graph_instance_id`` (captured at construction). All
    methods take ``node_name`` only.

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
    def begin_invocation(self, node_name: str) -> InvocationContext:
        """Begin a new invocation. INSERT a new RUNNING record.

        If a prior non-suspended RUNNING record exists, mark it CRASHED
        (orphan cleanup). A prior suspended RUNNING is left in place
        (valid rebuild source). Records begin directly as RUNNING.

        Returns the ``InvocationContext`` with invocation_id, version,
        and parent_version.
        """
        ...

    @abstractmethod
    def complete_invocation(self, invocation: InvocationContext, state: dict[str, Any]) -> None:
        """Mark an invocation COMPLETED. STRICT CAS — raises on lost race."""
        ...

    @abstractmethod
    def suspend_invocation(self, invocation: InvocationContext, snapshot: dict[str, Any]) -> None:
        """Suspend an invocation (GraphInterrupt path).

        Status stays RUNNING, ``suspended`` set to True. STRICT CAS.
        """
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
        """Safety net: orphan non-suspended RUNNING → CRASHED. TOLERANT.

        Terminal and suspended records are left untouched.
        """
        ...

    # ── Query ───────────────────────────────────────────────────────────

    @abstractmethod
    def load_latest(self, node_name: str) -> NodeInvocationRecord | None:
        """Load the latest record for a node (highest version)."""
        ...

    @abstractmethod
    def load_latest_completed(self, node_name: str) -> NodeInvocationRecord | None:
        """Load the latest COMPLETED record for a node."""
        ...

    @abstractmethod
    def load_by_invocation_id(
        self, node_name: str, invocation_id: int
    ) -> NodeInvocationRecord | None:
        """Load a record by its ``invocation_id`` within a node's version chain.

        Returns ``None`` if no record for ``node_name`` has the given
        ``invocation_id``. Used by recovery to check whether a specific
        consuming invocation is COMPLETED (auto-promote delivers).
        """
        ...

    @abstractmethod
    def query_versions(
        self,
        node_name: str,
        status_filter: set[InvocationStatus] | None = None,
    ) -> list[NodeInvocationRecord]:
        """Query versions for a node, optionally filtered by status."""
        ...

    @abstractmethod
    def list_nodes(self) -> list[str]:
        """List all node names that have state snapshots."""
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

    def begin_invocation(self, node_name: str) -> InvocationContext:
        invocation_id = default_id_generator().generate()
        return InvocationContext(
            invocation_id=invocation_id,
            node_name=node_name,
            version=0,
            parent_version=None,
        )

    def complete_invocation(self, invocation: InvocationContext, state: dict[str, Any]) -> None:
        pass

    def suspend_invocation(self, invocation: InvocationContext, snapshot: dict[str, Any]) -> None:
        pass

    def crash_invocation(self, invocation: InvocationContext) -> None:
        pass

    def cancel_invocation(self, invocation: InvocationContext) -> None:
        pass

    def finalize_invocation(self, invocation: InvocationContext) -> None:
        pass

    def load_latest(self, node_name: str) -> NodeInvocationRecord | None:
        return None

    def load_latest_completed(self, node_name: str) -> NodeInvocationRecord | None:
        return None

    def load_by_invocation_id(
        self, node_name: str, invocation_id: int
    ) -> NodeInvocationRecord | None:
        return None

    def query_versions(
        self,
        node_name: str,
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

    Stores records in a dict keyed by ``node_name``. Each node has a list
    of ``NodeInvocationRecord`` ordered by version ascending. Lifecycle
    methods mutate records in place (CAS via status check).
    """

    def __init__(self, graph_instance_id: int) -> None:
        super().__init__(graph_instance_id)
        self._records: dict[str, list[NodeInvocationRecord]] = {}

    def begin_invocation(self, node_name: str) -> InvocationContext:
        gid = self._graph_instance_id
        records = self._records.get(node_name, [])

        # Orphan cleanup: prior non-suspended RUNNING → CRASHED.
        if records:
            latest = records[-1]
            if latest.status == InvocationStatus.RUNNING and not latest.suspended:
                ts = now_ms()
                crashed = latest.model_copy(
                    update={"status": InvocationStatus.CRASHED, "updated_at": ts}
                )
                records[-1] = crashed
                self._records[node_name] = records

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
            node_name=node_name,
            version=version,
            parent_version=parent_version,
            status=InvocationStatus.RUNNING,
            state_json={},
            suspended=False,
            created_at=ts,
            updated_at=ts,
        )
        records.append(record)
        self._records[node_name] = records

        return InvocationContext(
            invocation_id=invocation_id,
            node_name=node_name,
            version=version,
            parent_version=parent_version,
        )

    def _find_current(self, invocation: InvocationContext) -> NodeInvocationRecord | None:
        records = self._records.get(invocation.node_name, [])
        for r in records:
            if r.version == invocation.version:
                return r
        return None

    def complete_invocation(self, invocation: InvocationContext, state: dict[str, Any]) -> None:
        current = self._find_current(invocation)
        if current is None:
            raise InvocationStateError(
                f"No record found for {invocation.node_name!r} version {invocation.version}."
            )
        if current.status != InvocationStatus.RUNNING or current.suspended:
            raise InvocationStateError(
                f"CAS failed: {invocation.node_name!r} v{invocation.version} "
                f"is {current.status.value} (suspended={current.suspended}), "
                f"expected non-suspended RUNNING."
            )
        ts = now_ms()
        updated = current.model_copy(
            update={
                "status": InvocationStatus.COMPLETED,
                "state_json": state,
                "updated_at": ts,
            }
        )
        records = self._records[invocation.node_name]
        idx = records.index(current)
        records[idx] = updated

    def suspend_invocation(self, invocation: InvocationContext, snapshot: dict[str, Any]) -> None:
        current = self._find_current(invocation)
        if current is None:
            raise InvocationStateError(
                f"No record found for {invocation.node_name!r} version {invocation.version}."
            )
        if current.status != InvocationStatus.RUNNING or current.suspended:
            raise InvocationStateError(
                f"CAS failed: {invocation.node_name!r} v{invocation.version} "
                f"is {current.status.value} (suspended={current.suspended}), "
                f"expected non-suspended RUNNING."
            )
        ts = now_ms()
        updated = current.model_copy(
            update={
                "status": InvocationStatus.RUNNING,
                "state_json": snapshot,
                "suspended": True,
                "updated_at": ts,
            }
        )
        records = self._records[invocation.node_name]
        idx = records.index(current)
        records[idx] = updated

    def cancel_invocation(self, invocation: InvocationContext) -> None:
        current = self._find_current(invocation)
        if current is None:
            raise InvocationStateError(
                f"No record found for {invocation.node_name!r} version {invocation.version}."
            )
        if current.status != InvocationStatus.RUNNING or current.suspended:
            raise InvocationStateError(
                f"CAS failed: {invocation.node_name!r} v{invocation.version} "
                f"is {current.status.value} (suspended={current.suspended}), "
                f"expected non-suspended RUNNING."
            )
        ts = now_ms()
        updated = current.model_copy(
            update={
                "status": InvocationStatus.CANCELED,
                "updated_at": ts,
            }
        )
        records = self._records[invocation.node_name]
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
        records = self._records[invocation.node_name]
        idx = records.index(current)
        records[idx] = updated

    def finalize_invocation(self, invocation: InvocationContext) -> None:
        current = self._find_current(invocation)
        if current is None:
            return
        # Suspended RUNNING — don't touch.
        if current.status == InvocationStatus.RUNNING and current.suspended:
            return
        # Terminal — don't touch.
        if current.status in (
            InvocationStatus.COMPLETED,
            InvocationStatus.CANCELED,
            InvocationStatus.CRASHED,
        ):
            return
        # Orphan non-suspended RUNNING → CRASHED.
        ts = now_ms()
        updated = current.model_copy(
            update={
                "status": InvocationStatus.CRASHED,
                "updated_at": ts,
            }
        )
        records = self._records[invocation.node_name]
        idx = records.index(current)
        records[idx] = updated

    def load_latest(self, node_name: str) -> NodeInvocationRecord | None:
        records = self._records.get(node_name, [])
        if not records:
            return None
        return records[-1]

    def load_latest_completed(self, node_name: str) -> NodeInvocationRecord | None:
        records = self._records.get(node_name, [])
        completed = [r for r in records if r.status == InvocationStatus.COMPLETED]
        if not completed:
            return None
        return max(completed, key=lambda r: r.version)

    def load_by_invocation_id(
        self, node_name: str, invocation_id: int
    ) -> NodeInvocationRecord | None:
        records = self._records.get(node_name, [])
        for r in records:
            if r.invocation_id == invocation_id:
                return r
        return None

    def query_versions(
        self,
        node_name: str,
        status_filter: set[InvocationStatus] | None = None,
    ) -> list[NodeInvocationRecord]:
        records = self._records.get(node_name, [])
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
        return sorted(result, key=lambda r: (r.node_name, r.version), reverse=True)

    def clear(self) -> None:
        self._records.clear()


# ── SQLite ───────────────────────────────────────────────────────────────


class SqliteNodeStateStore(NodeStateStore):
    """SQLite-backed ``NodeStateStore`` with CAS semantics.

    Uses ``UPDATE ... WHERE status='running' AND suspended=0`` for strict
    transitions (complete / suspend / cancel). ``rowcount == 0`` indicates
    a lost race → ``InvocationStateError``.

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

        The DDL matches `001_initial.sql` table 18 (including the `status`
        CHECK constraint and the ``UNIQUE (graph_instance_id, node_name,
        version)`` constraint). The ``json_valid(state_json)`` CHECK from
        the migration is omitted here — JSON1 may not be compiled in on
        all standalone builds; the migration includes it for workspace DBs
        where JSON1 is guaranteed (same convention as
        ``SqliteGraphSpecStore`` and ``SqliteDeliverStore``).
        """
        conn = self._conn
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {_NODE_STATE_TABLE} ("
            f"{_COL_NODE_STATE_ID} INTEGER PRIMARY KEY, "
            f"{_COL_GRAPH_INSTANCE_ID} INTEGER NOT NULL, "
            f"{_COL_NODE_NAME} TEXT NOT NULL, "
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
            f"{_COL_STATE_JSON} TEXT NOT NULL, "
            f"{_COL_SUSPENDED} INTEGER NOT NULL DEFAULT 0, "
            f"{_COL_CREATED_AT} INTEGER NOT NULL, "
            f"{_COL_UPDATED_AT} INTEGER NOT NULL, "
            f"UNIQUE ({_COL_GRAPH_INSTANCE_ID}, {_COL_NODE_NAME}, {_COL_VERSION})"
            f")"
        )
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{_NODE_STATE_TABLE}_latest "
            f"ON {_NODE_STATE_TABLE} "
            f"({_COL_GRAPH_INSTANCE_ID}, {_COL_NODE_NAME}, {_COL_VERSION} DESC)"
        )
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{_NODE_STATE_TABLE}_node "
            f"ON {_NODE_STATE_TABLE} ({_COL_GRAPH_INSTANCE_ID}, {_COL_NODE_NAME})"
        )
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{_NODE_STATE_TABLE}_status "
            f"ON {_NODE_STATE_TABLE} ({_COL_GRAPH_INSTANCE_ID}, {_COL_NODE_NAME}, {_COL_STATUS})"
        )
        conn.commit()

    # ── Lifecycle ───────────────────────────────────────────────────────

    def begin_invocation(self, node_name: str) -> InvocationContext:
        gid = self._graph_instance_id
        conn = self._conn

        # Orphan cleanup: prior non-suspended RUNNING → CRASHED (tolerant CAS).
        latest = self.load_latest(node_name)
        if latest is not None and latest.status == InvocationStatus.RUNNING and not latest.suspended:
            ts = now_ms()
            conn.execute(
                f"UPDATE {_NODE_STATE_TABLE} "
                f"SET {_COL_STATUS} = ?, {_COL_STATE_JSON} = ?, {_COL_SUSPENDED} = 0, {_COL_UPDATED_AT} = ? "
                f"WHERE {_COL_GRAPH_INSTANCE_ID} = ? AND {_COL_NODE_NAME} = ? "
                f"AND {_COL_VERSION} = ? AND {_COL_STATUS} = ? AND {_COL_SUSPENDED} = 0",
                (
                    InvocationStatus.CRASHED.value,
                    json.dumps({}),
                    ts,
                    gid, node_name, latest.version,
                    InvocationStatus.RUNNING.value,
                ),
            )
            conn.commit()

        # version = max(all existing versions) + 1.
        row = conn.execute(
            f"SELECT MAX({_COL_VERSION}) FROM {_NODE_STATE_TABLE} "
            f"WHERE {_COL_GRAPH_INSTANCE_ID} = ? AND {_COL_NODE_NAME} = ?",
            (gid, node_name),
        ).fetchone()
        version = (row[0] + 1) if row[0] is not None else 0

        # parent_version from load_latest_completed.
        latest_completed = self.load_latest_completed(node_name)
        parent_version = latest_completed.version if latest_completed is not None else None

        invocation_id = default_id_generator().generate()
        node_state_id = default_id_generator().generate()
        ts = now_ms()
        conn.execute(
            f"INSERT INTO {_NODE_STATE_TABLE} "
            f"({_COL_NODE_STATE_ID}, {_COL_GRAPH_INSTANCE_ID}, {_COL_NODE_NAME}, "
            f"{_COL_VERSION}, {_COL_PARENT_VERSION}, {_COL_STATUS}, "
            f"{_COL_INVOCATION_ID}, {_COL_STATE_JSON}, {_COL_SUSPENDED}, "
            f"{_COL_CREATED_AT}, {_COL_UPDATED_AT}) "
            f"VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                node_state_id,
                gid,
                node_name,
                version,
                parent_version,
                InvocationStatus.RUNNING.value,
                invocation_id,
                json.dumps({}),
                0,
                ts,
                ts,
            ),
        )
        conn.commit()

        return InvocationContext(
            invocation_id=invocation_id,
            node_name=node_name,
            version=version,
            parent_version=parent_version,
        )

    def complete_invocation(self, invocation: InvocationContext, state: dict[str, Any]) -> None:
        self._strict_update(
            invocation,
            status=InvocationStatus.COMPLETED,
            state_json=state,
            suspended=None,
        )

    def suspend_invocation(self, invocation: InvocationContext, snapshot: dict[str, Any]) -> None:
        self._strict_update(
            invocation,
            status=InvocationStatus.RUNNING,
            state_json=snapshot,
            suspended=True,
        )

    def cancel_invocation(self, invocation: InvocationContext) -> None:
        self._strict_update(
            invocation,
            status=InvocationStatus.CANCELED,
            state_json=None,
            suspended=None,
        )

    def _strict_update(
        self,
        invocation: InvocationContext,
        *,
        status: InvocationStatus,
        state_json: dict[str, Any] | None,
        suspended: bool | None,
    ) -> None:
        """STRICT CAS: UPDATE only if status='running' AND suspended=0.

        Raises ``InvocationStateError`` if rowcount=0 (lost race or
        already terminal).
        """
        gid = self._graph_instance_id
        conn = self._conn
        ts = now_ms()

        set_clauses = [
            f"{_COL_STATUS} = ?",
            f"{_COL_UPDATED_AT} = ?",
        ]
        params: list[Any] = [status.value, ts]

        if state_json is not None:
            set_clauses.append(f"{_COL_STATE_JSON} = ?")
            params.append(json.dumps(state_json))
        if suspended is not None:
            set_clauses.append(f"{_COL_SUSPENDED} = ?")
            params.append(1 if suspended else 0)

        params.extend([gid, invocation.node_name, invocation.version])

        cursor = conn.execute(
            f"UPDATE {_NODE_STATE_TABLE} "
            f"SET {', '.join(set_clauses)} "
            f"WHERE {_COL_GRAPH_INSTANCE_ID} = ? AND {_COL_NODE_NAME} = ? "
            f"AND {_COL_VERSION} = ? "
            f"AND {_COL_STATUS} = ? AND {_COL_SUSPENDED} = 0",
            (*params, InvocationStatus.RUNNING.value),
        )
        conn.commit()

        if cursor.rowcount == 0:
            current = self.load_latest(invocation.node_name)
            cur_status = current.status.value if current else "missing"
            cur_suspended = current.suspended if current else False
            raise InvocationStateError(
                f"CAS failed: {invocation.node_name!r} v{invocation.version} "
                f"is {cur_status} (suspended={cur_suspended}), "
                f"expected non-suspended RUNNING."
            )

    def crash_invocation(self, invocation: InvocationContext) -> None:
        """TOLERANT: update RUNNING → CRASHED. No-op if already terminal."""
        gid = self._graph_instance_id
        conn = self._conn
        ts = now_ms()
        conn.execute(
            f"UPDATE {_NODE_STATE_TABLE} "
            f"SET {_COL_STATUS} = ?, {_COL_STATE_JSON} = ?, {_COL_SUSPENDED} = 0, {_COL_UPDATED_AT} = ? "
            f"WHERE {_COL_GRAPH_INSTANCE_ID} = ? AND {_COL_NODE_NAME} = ? "
            f"AND {_COL_VERSION} = ? AND {_COL_STATUS} = ?",
            (
                InvocationStatus.CRASHED.value,
                json.dumps({}),
                ts,
                gid,
                invocation.node_name,
                invocation.version,
                InvocationStatus.RUNNING.value,
            ),
        )
        conn.commit()

    def finalize_invocation(self, invocation: InvocationContext) -> None:
        """TOLERANT: orphan non-suspended RUNNING → CRASHED.

        Terminal and suspended records are left untouched.
        """
        gid = self._graph_instance_id
        conn = self._conn
        ts = now_ms()
        conn.execute(
            f"UPDATE {_NODE_STATE_TABLE} "
            f"SET {_COL_STATUS} = ?, {_COL_STATE_JSON} = ?, {_COL_SUSPENDED} = 0, {_COL_UPDATED_AT} = ? "
            f"WHERE {_COL_GRAPH_INSTANCE_ID} = ? AND {_COL_NODE_NAME} = ? "
            f"AND {_COL_VERSION} = ? AND {_COL_STATUS} = ? AND {_COL_SUSPENDED} = 0",
            (
                InvocationStatus.CRASHED.value,
                json.dumps({}),
                ts,
                gid,
                invocation.node_name,
                invocation.version,
                InvocationStatus.RUNNING.value,
            ),
        )
        conn.commit()

    # ── Query ───────────────────────────────────────────────────────────

    def load_latest(self, node_name: str) -> NodeInvocationRecord | None:
        gid = self._graph_instance_id
        row = self._conn.execute(
            f"SELECT {_SELECT_COLUMNS} FROM {_NODE_STATE_TABLE} "
            f"WHERE {_COL_GRAPH_INSTANCE_ID} = ? AND {_COL_NODE_NAME} = ? "
            f"ORDER BY {_COL_VERSION} DESC LIMIT 1",
            (gid, node_name),
        ).fetchone()
        return self._row_to_record(row) if row is not None else None

    def load_latest_completed(self, node_name: str) -> NodeInvocationRecord | None:
        gid = self._graph_instance_id
        row = self._conn.execute(
            f"SELECT {_SELECT_COLUMNS} FROM {_NODE_STATE_TABLE} "
            f"WHERE {_COL_GRAPH_INSTANCE_ID} = ? AND {_COL_NODE_NAME} = ? "
            f"AND {_COL_STATUS} = ? "
            f"ORDER BY {_COL_VERSION} DESC LIMIT 1",
            (gid, node_name, InvocationStatus.COMPLETED.value),
        ).fetchone()
        return self._row_to_record(row) if row is not None else None

    def load_by_invocation_id(
        self, node_name: str, invocation_id: int
    ) -> NodeInvocationRecord | None:
        gid = self._graph_instance_id
        row = self._conn.execute(
            f"SELECT {_SELECT_COLUMNS} FROM {_NODE_STATE_TABLE} "
            f"WHERE {_COL_GRAPH_INSTANCE_ID} = ? AND {_COL_NODE_NAME} = ? "
            f"AND {_COL_INVOCATION_ID} = ? "
            f"ORDER BY {_COL_VERSION} DESC LIMIT 1",
            (gid, node_name, invocation_id),
        ).fetchone()
        return self._row_to_record(row) if row is not None else None

    def query_versions(
        self,
        node_name: str,
        status_filter: set[InvocationStatus] | None = None,
    ) -> list[NodeInvocationRecord]:
        gid = self._graph_instance_id
        if status_filter is None:
            rows = self._conn.execute(
                f"SELECT {_SELECT_COLUMNS} FROM {_NODE_STATE_TABLE} "
                f"WHERE {_COL_GRAPH_INSTANCE_ID} = ? AND {_COL_NODE_NAME} = ? "
                f"ORDER BY {_COL_VERSION} DESC",
                (gid, node_name),
            ).fetchall()
        else:
            placeholders = ",".join("?" for _ in status_filter)
            rows = self._conn.execute(
                f"SELECT {_SELECT_COLUMNS} FROM {_NODE_STATE_TABLE} "
                f"WHERE {_COL_GRAPH_INSTANCE_ID} = ? AND {_COL_NODE_NAME} = ? "
                f"AND {_COL_STATUS} IN ({placeholders}) "
                f"ORDER BY {_COL_VERSION} DESC",
                (gid, node_name, *[s.value for s in status_filter]),
            ).fetchall()
        return [self._row_to_record(row) for row in rows]

    def list_nodes(self) -> list[str]:
        gid = self._graph_instance_id
        rows = self._conn.execute(
            f"SELECT DISTINCT {_COL_NODE_NAME} FROM {_NODE_STATE_TABLE} "
            f"WHERE {_COL_GRAPH_INSTANCE_ID} = ? "
            f"ORDER BY {_COL_NODE_NAME}",
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
            f"ORDER BY {_COL_NODE_NAME}, {_COL_VERSION} DESC",
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


__all__ = [
    "NodeStateStore",
    "NullNodeStateStore",
    "InMemoryNodeStateStore",
    "SqliteNodeStateStore",
]
