"""Persistence contract and implementations for session-tree nodes."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from pathlib import Path
from sqlite3 import Row
from typing import TYPE_CHECKING, Final

from modex_agent.core.scope import RecordScope
from modex_agent.core.session_store import safe_filename
from modex_agent.multi_agent.session_tree.models import (
    NodeVersionStatus,
    TreeNodeRecord,
)
from modex_agent.utils.file_io import atomic_write_text
from modex_agent.utils.time import now_ms

if TYPE_CHECKING:
    from modex_agent.persistence.connection import ConnectionManager, Transaction

__all__ = [
    "InMemoryTreeNodeStore",
    "LocalFileTreeNodeStore",
    "SqliteTreeNodeStore",
    "TreeNodeStore",
]

_NODE_COLUMNS: Final = (
    "tree_id, session_id, parent_session_id, agent_name, version, "
    "parent_version, status, created_at, updated_at"
)


class TreeNodeStore(ABC):
    """Storage contract for the single mutable record of each tree session."""

    @abstractmethod
    async def create(self, record: TreeNodeRecord) -> None:
        """Insert a node record."""
        ...

    @abstractmethod
    async def get(self, session_id: str) -> TreeNodeRecord | None:
        """Return the record for ``session_id``, if present."""
        ...

    @abstractmethod
    async def get_or_create(self, record: TreeNodeRecord) -> TreeNodeRecord:
        """Atomically return the existing record or insert and return ``record``."""
        ...

    @abstractmethod
    async def update_version(
        self,
        session_id: str,
        version: int,
        parent_version: int | None,
        status: NodeVersionStatus,
    ) -> None:
        """Update version fields on the existing record in place."""
        ...

    @abstractmethod
    async def get_tree_sessions(self, tree_id: str) -> list[str]:
        """Return every session id belonging to ``tree_id``."""
        ...

    @abstractmethod
    async def get_tree_node_records(self, tree_id: str) -> list[TreeNodeRecord]:
        """Return every node record belonging to ``tree_id``."""
        ...


class InMemoryTreeNodeStore(TreeNodeStore):
    """Process-local tree-node store with atomic async operations."""

    def __init__(self) -> None:
        self._records: dict[str, TreeNodeRecord] = {}
        self._lock = asyncio.Lock()

    async def create(self, record: TreeNodeRecord) -> None:
        async with self._lock:
            self._records[record.session_id] = record

    async def get(self, session_id: str) -> TreeNodeRecord | None:
        async with self._lock:
            return self._records.get(session_id)

    async def get_or_create(self, record: TreeNodeRecord) -> TreeNodeRecord:
        async with self._lock:
            existing = self._records.get(record.session_id)
            if existing is not None:
                return existing
            self._records[record.session_id] = record
            return record

    async def update_version(
        self,
        session_id: str,
        version: int,
        parent_version: int | None,
        status: NodeVersionStatus,
    ) -> None:
        async with self._lock:
            record = self._records.get(session_id)
            if record is None:
                return
            self._records[session_id] = record.model_copy(
                update={
                    "version": version,
                    "parent_version": parent_version,
                    "status": status,
                    "updated_at": now_ms(),
                }
            )

    async def get_tree_sessions(self, tree_id: str) -> list[str]:
        async with self._lock:
            return sorted(
                record.session_id
                for record in self._records.values()
                if record.tree_id == tree_id
            )

    async def get_tree_node_records(self, tree_id: str) -> list[TreeNodeRecord]:
        async with self._lock:
            return sorted(
                (record for record in self._records.values() if record.tree_id == tree_id),
                key=lambda record: record.session_id,
            )


class LocalFileTreeNodeStore(TreeNodeStore):
    """File-backed store using one Pydantic JSON record per session."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()

    def _path(self, session_id: str) -> Path:
        return self._root / f"{safe_filename(session_id)}.json"

    def _read(self, session_id: str) -> TreeNodeRecord | None:
        path = self._path(session_id)
        if not path.exists():
            return None
        return TreeNodeRecord.model_validate_json(path.read_text(encoding="utf-8"))

    def _write(self, record: TreeNodeRecord) -> None:
        atomic_write_text(self._path(record.session_id), record.model_dump_json())

    async def create(self, record: TreeNodeRecord) -> None:
        async with self._lock:
            await asyncio.to_thread(self._write, record)

    async def get(self, session_id: str) -> TreeNodeRecord | None:
        async with self._lock:
            return await asyncio.to_thread(self._read, session_id)

    async def get_or_create(self, record: TreeNodeRecord) -> TreeNodeRecord:
        def read_or_create() -> TreeNodeRecord:
            existing = self._read(record.session_id)
            if existing is not None:
                return existing
            self._write(record)
            return record

        async with self._lock:
            return await asyncio.to_thread(read_or_create)

    async def update_version(
        self,
        session_id: str,
        version: int,
        parent_version: int | None,
        status: NodeVersionStatus,
    ) -> None:
        def update() -> None:
            record = self._read(session_id)
            if record is None:
                return
            self._write(
                record.model_copy(
                    update={
                        "version": version,
                        "parent_version": parent_version,
                        "status": status,
                        "updated_at": now_ms(),
                    }
                )
            )

        async with self._lock:
            await asyncio.to_thread(update)

    async def get_tree_sessions(self, tree_id: str) -> list[str]:
        def collect() -> list[str]:
            records = (
                TreeNodeRecord.model_validate_json(path.read_text(encoding="utf-8"))
                for path in self._root.glob("*.json")
            )
            return sorted(record.session_id for record in records if record.tree_id == tree_id)

        async with self._lock:
            return await asyncio.to_thread(collect)

    async def get_tree_node_records(self, tree_id: str) -> list[TreeNodeRecord]:
        def collect() -> list[TreeNodeRecord]:
            records = [
                TreeNodeRecord.model_validate_json(path.read_text(encoding="utf-8"))
                for path in self._root.glob("*.json")
            ]
            return sorted(
                (record for record in records if record.tree_id == tree_id),
                key=lambda record: record.session_id,
            )

        async with self._lock:
            return await asyncio.to_thread(collect)


class SqliteTreeNodeStore(TreeNodeStore):
    """SQLite tree-node store scoped through ``RecordScope.canonical()``."""

    def __init__(self, connection: ConnectionManager, scope: RecordScope) -> None:
        self._connection = connection
        self._scope = scope
        self._owner_scope_key = scope.canonical()

    async def create(self, record: TreeNodeRecord) -> None:
        await self._insert(self._connection, record, or_ignore=False)

    async def get(self, session_id: str) -> TreeNodeRecord | None:
        row = await self._connection.query_one(
            f"SELECT {_NODE_COLUMNS} FROM tree_nodes "
            "WHERE owner_scope_key = ? AND session_id = ?",
            (self._owner_scope_key, session_id),
        )
        return None if row is None else _record_from_row(row)

    async def get_or_create(self, record: TreeNodeRecord) -> TreeNodeRecord:
        async with self._connection.transaction(immediate=True) as transaction:
            await self._insert(transaction, record, or_ignore=True)
            row = await transaction.query_one(
                f"SELECT {_NODE_COLUMNS} FROM tree_nodes "
                "WHERE owner_scope_key = ? AND session_id = ?",
                (self._owner_scope_key, record.session_id),
            )
            if row is None:
                raise RuntimeError(
                    "SqliteTreeNodeStore.get_or_create: row not found after "
                    "INSERT OR IGNORE — session_id="
                    + record.session_id
                )
            return _record_from_row(row)

    async def update_version(
        self,
        session_id: str,
        version: int,
        parent_version: int | None,
        status: NodeVersionStatus,
    ) -> None:
        await self._connection.execute(
            "UPDATE tree_nodes SET version = ?, parent_version = ?, status = ?, updated_at = ? "
            "WHERE owner_scope_key = ? AND session_id = ?",
            (version, parent_version, status.value, now_ms(), self._owner_scope_key, session_id),
        )

    async def get_tree_sessions(self, tree_id: str) -> list[str]:
        rows = await self._connection.query_all(
            "SELECT session_id FROM tree_nodes "
            "WHERE owner_scope_key = ? AND tree_id = ? ORDER BY session_id",
            (self._owner_scope_key, tree_id),
        )
        return [row["session_id"] for row in rows]

    async def get_tree_node_records(self, tree_id: str) -> list[TreeNodeRecord]:
        rows = await self._connection.query_all(
            f"SELECT {_NODE_COLUMNS} FROM tree_nodes "
            "WHERE owner_scope_key = ? AND tree_id = ? ORDER BY session_id",
            (self._owner_scope_key, tree_id),
        )
        return [_record_from_row(row) for row in rows]

    async def _insert(
        self,
        target: ConnectionManager | Transaction,
        record: TreeNodeRecord,
        *,
        or_ignore: bool,
    ) -> None:
        command = "INSERT OR IGNORE" if or_ignore else "INSERT"
        scope_key = self._scope.model_copy(
            update={
                "session_id": record.session_id,
                "parent_session_id": record.parent_session_id,
            }
        ).canonical()
        await target.execute(
            f"{command} INTO tree_nodes "
            "(tree_id, session_id, parent_session_id, agent_name, version, parent_version, "
            "status, scope_key, owner_scope_key, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.tree_id,
                record.session_id,
                record.parent_session_id,
                record.agent_name,
                record.version,
                record.parent_version,
                record.status.value,
                scope_key,
                self._owner_scope_key,
                record.created_at,
                record.updated_at,
            ),
        )


def _record_from_row(row: Row) -> TreeNodeRecord:
    return TreeNodeRecord.model_validate(dict(row))
