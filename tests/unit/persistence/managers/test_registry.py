"""T23 tests for RegistryPersistenceManager — registry DB lifecycle.

The manager owns the registry ``ConnectionManager`` and governs its
open/close lifecycle:

- ``open()`` opens the connection and runs pending registry migrations
  (creates ``workspaces`` + ``session_workspace_map``).
- ``close()`` performs the WAL checkpoint and closes the connection
  (called at ``BotService.stop()`` after all workspaces are evicted).
- Both are idempotent.
- ``open() -> close() -> open()`` preserves committed data (WAL replay).
- ``store`` exposes a ``SqliteWorkspaceRegistryStore`` bound to the
  manager's connection.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from modex_agent.persistence import DatabaseKind
from modex_agent.persistence.managers.registry import RegistryPersistenceManager

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _manager(tmp_path: Path) -> RegistryPersistenceManager:
    return RegistryPersistenceManager(tmp_path / "registry.db")


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class TestRegistryPersistenceManagerLifecycle:
    @pytest.mark.asyncio
    async def test_open_creates_registry_tables(self, tmp_path: Path) -> None:
        manager = _manager(tmp_path)
        try:
            await manager.open()
            workspaces = await manager.connection.query_value(
                "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'workspaces'",
                int,
            )
            session_map = await manager.connection.query_value(
                "SELECT COUNT(*) FROM sqlite_master "
                "WHERE type = 'table' AND name = 'session_workspace_map'",
                int,
            )
            assert workspaces == 1
            assert session_map == 1
        finally:
            await manager.close()

    @pytest.mark.asyncio
    async def test_open_is_idempotent(self, tmp_path: Path) -> None:
        manager = _manager(tmp_path)
        await manager.open()
        try:
            await manager.open()  # second open must be a no-op
        finally:
            await manager.close()

    @pytest.mark.asyncio
    async def test_close_is_idempotent(self, tmp_path: Path) -> None:
        manager = _manager(tmp_path)
        await manager.open()
        await manager.close()
        await manager.close()  # second close must be a no-op

    @pytest.mark.asyncio
    async def test_open_close_reopen_preserves_data(self, tmp_path: Path) -> None:
        from modex_agent.workspace.record import WorkspaceRecord

        manager = _manager(tmp_path)
        await manager.open()
        record = WorkspaceRecord(
            workspace_id="ws-survive",
            target_path=str((tmp_path / "proj").resolve()),
            display_name="Survivor",
            created_at=1735689600000,
            last_active=1735689600000,
        )
        await manager.store.upsert_workspace(record)
        await manager.close()

        # Reopen — WAL replay must restore the committed row.
        manager2 = _manager(tmp_path)
        await manager2.open()
        try:
            got = await manager2.store.get_workspace(str(tmp_path / "proj"))
            assert got is not None
            assert got.workspace_id == "ws-survive"
            assert got.display_name == "Survivor"
        finally:
            await manager2.close()


class TestRegistryPersistenceManagerStore:
    @pytest.mark.asyncio
    async def test_store_property_returns_working_store(self, tmp_path: Path) -> None:
        from modex_agent.workspace.record import WorkspaceRecord

        manager = _manager(tmp_path)
        await manager.open()
        try:
            record = WorkspaceRecord(
                workspace_id="ws-1",
                target_path=str((tmp_path / "proj").resolve()),
                created_at=1735689600000,
                last_active=1735689600000,
            )
            await manager.store.upsert_workspace(record)
            got = await manager.store.get_workspace(str(tmp_path / "proj"))
            assert got is not None
            assert got.workspace_id == "ws-1"
        finally:
            await manager.close()

    @pytest.mark.asyncio
    async def test_store_property_is_cached(self, tmp_path: Path) -> None:
        manager = _manager(tmp_path)
        await manager.open()
        try:
            assert manager.store is manager.store
        finally:
            await manager.close()

    @pytest.mark.asyncio
    async def test_connection_exposes_connection_manager(self, tmp_path: Path) -> None:
        from modex_agent.persistence import ConnectionManager

        manager = _manager(tmp_path)
        await manager.open()
        try:
            assert isinstance(manager.connection, ConnectionManager)
        finally:
            await manager.close()

    @pytest.mark.asyncio
    async def test_db_path_uses_registry_kind(self, tmp_path: Path) -> None:
        """The manager must select DatabaseKind.REGISTRY for migrations."""
        manager = _manager(tmp_path)
        await manager.open()
        try:
            # The registry migration creates session_workspace_map; if the kind
            # were wrong, this table would not exist.
            count = await manager.connection.query_value(
                "SELECT COUNT(*) FROM sqlite_master "
                "WHERE type = 'table' AND name = 'session_workspace_map'",
                int,
            )
            assert count == 1
        finally:
            await manager.close()
        # Reference DatabaseKind so the import is used (linter satisfaction).
        assert DatabaseKind.REGISTRY.value == "registry"
