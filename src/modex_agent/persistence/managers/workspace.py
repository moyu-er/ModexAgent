"""Per-workspace SQLite persistence lifecycle manager.

Opens the workspace DB (``DatabaseKind.WORKSPACE``) at workspace materialize
and closes it at evict (after pools/broker/terminals). Constructs DB-backed
:class:`~modex_agent.memory.core.split_stores.MemoryStoreBundle` instances
whose four fields point to four **independent** adapter instances (unlike the
file backend where all fields alias one ``DefaultScopedStorage``).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from modex_agent.memory.core.split_stores import MemoryStoreBundle
from modex_agent.persistence.adapters.archive_store import SqliteArchiveStore
from modex_agent.persistence.adapters.cursor_store import SqliteCursorStore
from modex_agent.persistence.adapters.kv_store import SqliteKVStore
from modex_agent.persistence.adapters.message_store import SqliteMessageStore
from modex_agent.persistence.connection import ConnectionManager
from modex_agent.persistence.migration import DatabaseKind
from modex_agent.persistence.session_artifacts import SqliteSessionDatabaseCleaner

if TYPE_CHECKING:
    from modex_agent.core.scope import RecordScope


class WorkspacePersistenceManager:
    """Owns the workspace ``ConnectionManager`` and constructs memory bundles."""

    def __init__(self, db_path: Path) -> None:
        self._connection = ConnectionManager(db_path, DatabaseKind.WORKSPACE)

    async def open(self) -> None:
        """Open the DB connection and run pending migrations."""
        await self._connection.open()

    async def close(self) -> None:
        """WAL-checkpoint and close the DB connection."""
        await self._connection.close()

    @property
    def connection(self) -> ConnectionManager:
        """The shared ``ConnectionManager`` for this workspace DB."""
        return self._connection

    def create_bundle(
        self,
        scope: RecordScope,
        *,
        with_archive: bool = True,
        ttl_seconds: float | None = None,
    ) -> MemoryStoreBundle:
        """Construct a DB-backed ``MemoryStoreBundle`` for *scope*.

        Four independent adapter instances share the manager's
        ``ConnectionManager``. ``with_archive=False`` omits the archive store
        (sessions without archival history).
        """
        kwargs: dict[str, float] = {}
        if ttl_seconds is not None:
            kwargs["ttl_seconds"] = ttl_seconds
        messages = SqliteMessageStore(self._connection, scope, **kwargs)
        kv = SqliteKVStore(self._connection, scope)
        cursors = SqliteCursorStore(self._connection, scope)
        archive: SqliteArchiveStore | None = None
        if with_archive:
            archive = SqliteArchiveStore(self._connection, scope)
        return MemoryStoreBundle(
            messages=messages,
            kv=kv,
            cursors=cursors,
            archive=archive,
        )

    def create_session_database_cleaner(self) -> SqliteSessionDatabaseCleaner:
        """Create a session cleaner borrowing this manager's connection."""
        return SqliteSessionDatabaseCleaner(self._connection)
