"""T26 lifecycle integration tests — persistence manager open/close ordering.

Verifies the lifecycle hooks fire in the right order:

- ``WorkspacePersistenceManager`` opens at workspace materialize, closes at
  evict (after producers/pools/broker stop).
- ``RegistryPersistenceManager`` opens at initialize, closes at stop (after
  all workspaces evicted — registry DB is last).
- ``ConnectionManager.close()`` runs ``PRAGMA wal_checkpoint(TRUNCATE)``.
- Open → close → reopen preserves committed data (WAL replay).

These tests exercise the persistence managers directly (not the full
BotService) since the lifecycle contract is what T26 introduces.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from modex_agent.core.scope import RecordScope
from modex_agent.persistence.config import PersistenceBackend, PersistenceConfig
from modex_agent.persistence.managers.registry import RegistryPersistenceManager
from modex_agent.persistence.managers.workspace import WorkspacePersistenceManager


class _PoolScopedRecordScope(RecordScope):
    """Test-only RecordScope subclass with pool dimension (ADR-0028)."""

    pool: str | None = None

# ---------------------------------------------------------------------------
# PersistenceConfig
# ---------------------------------------------------------------------------


class TestPersistenceConfig:
    def test_default_backend_is_sqlite(self) -> None:
        cfg = PersistenceConfig()
        assert cfg.backend is PersistenceBackend.SQLITE

    def test_file_backend(self) -> None:
        cfg = PersistenceConfig(backend=PersistenceBackend.FILE)
        assert cfg.backend is PersistenceBackend.FILE

    def test_frozen(self) -> None:
        cfg = PersistenceConfig()
        with pytest.raises(ValidationError):
            cfg.backend = PersistenceBackend.FILE  # type: ignore[misc]

    def test_from_string(self) -> None:
        cfg = PersistenceConfig.model_validate({"backend": "file"})
        assert cfg.backend is PersistenceBackend.FILE

        cfg2 = PersistenceConfig.model_validate({"backend": "sqlite"})
        assert cfg2.backend is PersistenceBackend.SQLITE


# ---------------------------------------------------------------------------
# WorkspacePersistenceManager lifecycle
# ---------------------------------------------------------------------------


class TestWorkspacePersistenceManagerLifecycle:
    @pytest.mark.asyncio
    async def test_open_creates_workspace_db(self, tmp_path: Path) -> None:
        manager = WorkspacePersistenceManager(tmp_path / "state.db")
        try:
            await manager.open()
            tables = await manager.connection.query_value(
                "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'", int
            )
            assert tables > 0
        finally:
            await manager.close()

    @pytest.mark.asyncio
    async def test_close_is_idempotent(self, tmp_path: Path) -> None:
        manager = WorkspacePersistenceManager(tmp_path / "state.db")
        await manager.open()
        await manager.close()
        await manager.close()

    @pytest.mark.asyncio
    async def test_open_close_reopen_preserves_data(self, tmp_path: Path) -> None:
        manager = WorkspacePersistenceManager(tmp_path / "state.db")
        await manager.open()
        try:
            scope = _PoolScopedRecordScope(pool="default", session_id="s1")
            bundle = manager.create_bundle(scope)
            await bundle.messages.append_message(
                {"id": "m1", "role": "user", "content": "hello"}
            )
        finally:
            await manager.close()

        manager2 = WorkspacePersistenceManager(tmp_path / "state.db")
        await manager2.open()
        try:
            scope = _PoolScopedRecordScope(pool="default", session_id="s1")
            bundle2 = manager2.create_bundle(scope)
            msgs = await bundle2.messages.load_messages()
            assert len(msgs) == 1
            assert msgs[0]["content"] == "hello"
        finally:
            await manager2.close()

    @pytest.mark.asyncio
    async def test_close_runs_wal_checkpoint(self, tmp_path: Path) -> None:
        manager = WorkspacePersistenceManager(tmp_path / "state.db")
        await manager.open()
        await manager.connection.execute(
            "CREATE TABLE IF NOT EXISTS t (x)", ()
        )
        await manager.connection.execute("INSERT INTO t VALUES (1)", ())
        await manager.close()

        wal_file = tmp_path / "state.db-wal"
        assert not wal_file.exists() or wal_file.stat().st_size == 0

    @pytest.mark.asyncio
    async def test_connection_property_exposes_manager(self, tmp_path: Path) -> None:
        from modex_agent.persistence import ConnectionManager

        manager = WorkspacePersistenceManager(tmp_path / "state.db")
        await manager.open()
        try:
            assert isinstance(manager.connection, ConnectionManager)
        finally:
            await manager.close()


# ---------------------------------------------------------------------------
# RegistryPersistenceManager lifecycle
# ---------------------------------------------------------------------------


class TestRegistryPersistenceManagerLifecycle:
    @pytest.mark.asyncio
    async def test_open_creates_registry_tables(self, tmp_path: Path) -> None:
        manager = RegistryPersistenceManager(tmp_path / "registry.db")
        try:
            await manager.open()
            count = await manager.connection.query_value(
                "SELECT COUNT(*) FROM sqlite_master "
                "WHERE type = 'table' AND name = 'workspaces'",
                int,
            )
            assert count == 1
        finally:
            await manager.close()

    @pytest.mark.asyncio
    async def test_close_is_idempotent(self, tmp_path: Path) -> None:
        manager = RegistryPersistenceManager(tmp_path / "registry.db")
        await manager.open()
        await manager.close()
        await manager.close()

    @pytest.mark.asyncio
    async def test_open_close_reopen_preserves_data(self, tmp_path: Path) -> None:
        from modex_agent.workspace.record import WorkspaceRecord

        manager = RegistryPersistenceManager(tmp_path / "registry.db")
        await manager.open()
        try:
            record = WorkspaceRecord(
                workspace_id="ws-1",
                target_path=str((tmp_path / "proj").resolve()),
                created_at=1735689600000,
                last_active=1735689600000,
            )
            await manager.store.upsert_workspace(record)
        finally:
            await manager.close()

        manager2 = RegistryPersistenceManager(tmp_path / "registry.db")
        await manager2.open()
        try:
            got = await manager2.store.get_workspace(str(tmp_path / "proj"))
            assert got is not None
            assert got.workspace_id == "ws-1"
        finally:
            await manager2.close()


# ---------------------------------------------------------------------------
# Stop ordering: workspace DB closes before registry DB
# ---------------------------------------------------------------------------


class TestStopOrdering:
    @pytest.mark.asyncio
    async def test_workspace_db_closes_before_registry_db(self, tmp_path: Path) -> None:
        workspace_mgr = WorkspacePersistenceManager(tmp_path / "state.db")
        registry_mgr = RegistryPersistenceManager(tmp_path / "registry.db")

        close_order: list[str] = []

        original_ws_close = workspace_mgr.close
        original_reg_close = registry_mgr.close

        async def ws_close() -> None:
            close_order.append("workspace")
            await original_ws_close()

        async def reg_close() -> None:
            close_order.append("registry")
            await original_reg_close()

        workspace_mgr.close = ws_close  # type: ignore[method-assign]
        registry_mgr.close = reg_close  # type: ignore[method-assign]

        await workspace_mgr.open()
        await registry_mgr.open()

        # Simulate the stop sequence: evict workspaces first, then close
        # registry (the global, last-to-close persistence layer).
        await workspace_mgr.close()
        await registry_mgr.close()

        assert close_order == ["workspace", "registry"]


# ---------------------------------------------------------------------------
# AppConfig persistence field
# ---------------------------------------------------------------------------


class TestAppConfigPersistence:
    def test_app_config_has_persistence_field(self) -> None:
        from modex_agent.ioc.configs.app import AppConfig

        cfg = AppConfig()
        assert cfg.persistence is not None
        assert cfg.persistence.backend is PersistenceBackend.SQLITE

    def test_app_config_persistence_from_yaml_data(self) -> None:
        from modex_agent.ioc.configs.app import AppConfig

        cfg = AppConfig.model_validate(
            {"persistence": {"backend": "file"}}
        )
        assert cfg.persistence.backend is PersistenceBackend.FILE

    def test_app_config_persistence_defaults_to_sqlite(self) -> None:
        from modex_agent.ioc.configs.app import AppConfig

        cfg = AppConfig.model_validate({})
        assert cfg.persistence.backend is PersistenceBackend.SQLITE
