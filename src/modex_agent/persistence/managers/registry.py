"""Registry SQLite persistence lifecycle manager (T23).

Owns the registry ``ConnectionManager`` (``DatabaseKind.REGISTRY``) and
governs its open/close lifecycle:

- :meth:`open` at ``BotService.initialize()`` — opens the connection and runs
  pending registry migrations (creates ``workspaces`` +
  ``session_workspace_map``).
- :meth:`close` at ``BotService.stop()`` — WAL-checkpoint and close, called
  AFTER all workspaces are evicted (the registry DB is the last to close).

The :attr:`store` property exposes a
:class:`~modex_agent.persistence.adapters.workspace_registry_store.SqliteScopeRegistryStore`
bound to the manager's connection for workspace CRUD and session->workspace
routing.
"""

from __future__ import annotations

from pathlib import Path

from modex_agent.persistence.adapters.workspace_registry_store import (
    SqliteScopeRegistryStore,
)
from modex_agent.persistence.connection import ConnectionManager
from modex_agent.persistence.migration import DatabaseKind


class RegistryPersistenceManager:
    """Owns the registry ``ConnectionManager`` and its store adapter."""

    def __init__(self, db_path: Path) -> None:
        self._connection = ConnectionManager(db_path, DatabaseKind.REGISTRY)
        self._store = SqliteScopeRegistryStore(self._connection)

    async def open(self) -> None:
        """Open the registry DB connection and run pending migrations."""
        await self._connection.open()

    async def close(self) -> None:
        """WAL-checkpoint and close the registry DB connection.

        Idempotent. Must be called after all workspaces are evicted (the
        registry DB is the global, last-to-close persistence layer).
        """
        await self._connection.close()

    @property
    def connection(self) -> ConnectionManager:
        """The shared registry ``ConnectionManager``."""
        return self._connection

    @property
    def store(self) -> SqliteScopeRegistryStore:
        """The workspace registry + session-map store bound to this manager."""
        return self._store
