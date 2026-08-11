"""Persistence contract and implementations for session trees."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from pathlib import Path
from sqlite3 import Row
from typing import TYPE_CHECKING, Final, assert_never

from modex_agent.core.scope import RecordScope
from modex_agent.core.session_store import atomic_write_text, safe_filename
from modex_agent.multi_agent.session_tree.models import (
    SessionTreeRecord,
    SessionTreeStatus,
)
from modex_agent.utils.time import now_ms

if TYPE_CHECKING:
    from modex_agent.persistence.connection import ConnectionManager

__all__ = [
    "InMemorySessionTreeStore",
    "LocalFileSessionTreeStore",
    "SessionTreeStore",
    "SqliteSessionTreeStore",
]

_TREE_COLUMNS: Final = (
    "tree_id, root_node_session_id, pool_name, workspace_root, status, "
    "created_at, updated_at, completed_at"
)


class SessionTreeStore(ABC):
    """Storage contract for session-tree lifecycle records."""

    @abstractmethod
    async def create(self, record: SessionTreeRecord) -> None:
        """Insert a session-tree record."""
        ...

    @abstractmethod
    async def get(self, tree_id: str) -> SessionTreeRecord | None:
        """Return the record for ``tree_id``, if present."""
        ...

    @abstractmethod
    async def update_status(
        self,
        tree_id: str,
        status: SessionTreeStatus,
    ) -> None:
        """Update the lifecycle status of an existing tree."""
        ...

    @abstractmethod
    async def list_active(self) -> list[SessionTreeRecord]:
        """Return all active session trees."""
        ...


def _updated_record(
    record: SessionTreeRecord,
    status: SessionTreeStatus,
) -> SessionTreeRecord:
    timestamp = now_ms()
    match status:
        case SessionTreeStatus.ACTIVE:
            completed_at = None
        case SessionTreeStatus.COMPLETED | SessionTreeStatus.CANCELLED:
            completed_at = timestamp
        case unreachable:
            assert_never(unreachable)
    return record.model_copy(
        update={
            "status": status,
            "updated_at": timestamp,
            "completed_at": completed_at,
        }
    )


class InMemorySessionTreeStore(SessionTreeStore):
    """Process-local session-tree store with atomic async operations."""

    def __init__(self) -> None:
        self._records: dict[str, SessionTreeRecord] = {}
        self._lock = asyncio.Lock()

    async def create(self, record: SessionTreeRecord) -> None:
        async with self._lock:
            self._records[record.tree_id] = record

    async def get(self, tree_id: str) -> SessionTreeRecord | None:
        async with self._lock:
            return self._records.get(tree_id)

    async def update_status(
        self,
        tree_id: str,
        status: SessionTreeStatus,
    ) -> None:
        async with self._lock:
            record = self._records.get(tree_id)
            if record is None:
                return
            self._records[tree_id] = _updated_record(record, status)

    async def list_active(self) -> list[SessionTreeRecord]:
        async with self._lock:
            return sorted(
                (
                    record
                    for record in self._records.values()
                    if record.status is SessionTreeStatus.ACTIVE
                ),
                key=lambda record: record.tree_id,
            )


class LocalFileSessionTreeStore(SessionTreeStore):
    """File-backed store using one Pydantic JSON record per tree."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()

    def _path(self, tree_id: str) -> Path:
        return self._root / f"{safe_filename(tree_id)}.json"

    def _read(self, tree_id: str) -> SessionTreeRecord | None:
        path = self._path(tree_id)
        if not path.exists():
            return None
        return SessionTreeRecord.model_validate_json(path.read_text(encoding="utf-8"))

    def _write(self, record: SessionTreeRecord) -> None:
        atomic_write_text(self._path(record.tree_id), record.model_dump_json())

    async def create(self, record: SessionTreeRecord) -> None:
        async with self._lock:
            await asyncio.to_thread(self._write, record)

    async def get(self, tree_id: str) -> SessionTreeRecord | None:
        async with self._lock:
            return await asyncio.to_thread(self._read, tree_id)

    async def update_status(
        self,
        tree_id: str,
        status: SessionTreeStatus,
    ) -> None:
        def update() -> None:
            record = self._read(tree_id)
            if record is not None:
                self._write(_updated_record(record, status))

        async with self._lock:
            await asyncio.to_thread(update)

    async def list_active(self) -> list[SessionTreeRecord]:
        def collect() -> list[SessionTreeRecord]:
            records = (
                SessionTreeRecord.model_validate_json(path.read_text(encoding="utf-8"))
                for path in self._root.glob("*.json")
            )
            return sorted(
                (
                    record
                    for record in records
                    if record.status is SessionTreeStatus.ACTIVE
                ),
                key=lambda record: record.tree_id,
            )

        async with self._lock:
            return await asyncio.to_thread(collect)


class SqliteSessionTreeStore(SessionTreeStore):
    """SQLite session-tree store scoped through ``RecordScope.canonical()``."""

    def __init__(self, connection: ConnectionManager, scope: RecordScope) -> None:
        self._connection = connection
        self._scope = scope
        self._owner_scope_key = scope.canonical()

    async def create(self, record: SessionTreeRecord) -> None:
        scope_key = self._scope.model_copy(
            update={"session_id": record.root_node_session_id}
        ).canonical()
        await self._connection.execute(
            "INSERT INTO session_trees ("
            "tree_id, root_node_session_id, pool_name, workspace_root, scope_key, "
            "owner_scope_key, status, created_at, updated_at, completed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.tree_id,
                record.root_node_session_id,
                record.pool_name,
                record.workspace_root,
                scope_key,
                self._owner_scope_key,
                record.status.value,
                record.created_at,
                record.updated_at,
                record.completed_at,
            ),
        )

    async def get(self, tree_id: str) -> SessionTreeRecord | None:
        row = await self._connection.query_one(
            f"SELECT {_TREE_COLUMNS} FROM session_trees "
            "WHERE owner_scope_key = ? AND tree_id = ?",
            (self._owner_scope_key, tree_id),
        )
        return None if row is None else _record_from_row(row)

    async def update_status(
        self,
        tree_id: str,
        status: SessionTreeStatus,
    ) -> None:
        timestamp = now_ms()
        match status:
            case SessionTreeStatus.ACTIVE:
                completed_at = None
            case SessionTreeStatus.COMPLETED | SessionTreeStatus.CANCELLED:
                completed_at = timestamp
            case unreachable:
                assert_never(unreachable)
        await self._connection.execute(
            "UPDATE session_trees SET status = ?, updated_at = ?, completed_at = ? "
            "WHERE owner_scope_key = ? AND tree_id = ?",
            (status.value, timestamp, completed_at, self._owner_scope_key, tree_id),
        )

    async def list_active(self) -> list[SessionTreeRecord]:
        rows = await self._connection.query_all(
            f"SELECT {_TREE_COLUMNS} FROM session_trees "
            "WHERE owner_scope_key = ? AND status = ? ORDER BY tree_id",
            (self._owner_scope_key, SessionTreeStatus.ACTIVE.value),
        )
        return [_record_from_row(row) for row in rows]


def _record_from_row(row: Row) -> SessionTreeRecord:
    return SessionTreeRecord.model_validate(dict(row))
