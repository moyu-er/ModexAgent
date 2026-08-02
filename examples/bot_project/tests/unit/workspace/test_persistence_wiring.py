from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bot.workspace.wiring.resources import _build_resources, _stop_resources

from modex_agent.core.session_registry import SessionRegistry
from modex_agent.ioc.configs.app import AppConfig
from modex_agent.multi_agent.pool_router import PoolRoutingStore
from modex_agent.persistence.adapters.session_store import SqliteSessionStore
from modex_agent.persistence.connection import ConnectionNotOpenError
from modex_agent.persistence.managers import WorkspacePersistenceManager
from modex_agent.workspace.context import WorkspaceContext


def _service(home: Path, app_config: AppConfig) -> MagicMock:
    service = MagicMock()
    service._project_dir = home
    service.project_dir = home
    service._app_config = app_config
    service._home_persistence = None
    service._mcp_registry = None
    service._bot_model_config = None
    service._default_provider = None
    service._default_pool_name = "default"
    service._pool_session_store = MagicMock(spec=PoolRoutingStore)
    service._model_choice_registry = None
    service._transcript_store = None
    service._output_adapter_factory = None
    service._on_subagent_created = None
    service.control_channel = MagicMock()
    service.command_processor = MagicMock()
    service._collect_run_hooks.return_value = []
    service._build_hook_runner.return_value = MagicMock()
    service._pool_for_agent.side_effect = lambda agent: agent
    return service


async def test_home_resources_borrow_home_manager_and_use_sqlite_session_store(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    app_config = AppConfig.model_validate({"persistence": {"backend": "sqlite"}})
    service = _service(home, app_config)
    manager = WorkspacePersistenceManager(home / ".modex" / "state.db")
    await manager.open()
    service._home_persistence = manager
    ctx = WorkspaceContext.from_target(home, data_dir_name=".modex", home=home)

    with patch("bot.workspace.wiring.resources.PoolStore") as pool_store_type:
        pool_store_type.return_value.list_pools.return_value = []
        resources = await _build_resources(service, ctx)

    try:
        assert resources.persistence is manager
        assert resources.owns_persistence is False
        assert isinstance(resources.session_index_store, SqliteSessionStore)
        assert resources.pool_router is not None
        assert resources.pool_router._session_store is service._pool_session_store
    finally:
        await _stop_resources(resources)

    await manager.connection.query_value(
        "SELECT COUNT(*) FROM sqlite_master",
        int,
    )
    await manager.close()


async def test_non_home_resources_own_and_close_workspace_manager(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    target = tmp_path / "other"
    home.mkdir()
    target.mkdir()
    app_config = AppConfig.model_validate({"persistence": {"backend": "sqlite"}})
    service = _service(home, app_config)
    ctx = WorkspaceContext.from_target(target, data_dir_name=".modex", home=home)

    with patch("bot.workspace.wiring.resources.PoolStore") as pool_store_type:
        pool_store_type.return_value.list_pools.return_value = []
        resources = await _build_resources(service, ctx)

    assert resources.persistence is not None
    assert resources.owns_persistence is True
    assert resources.pool_router is not None
    assert resources.pool_router._session_store is service._pool_session_store
    connection = resources.persistence.connection

    await _stop_resources(resources)

    with pytest.raises(ConnectionNotOpenError):
        await connection.query_value("SELECT 1", int)


async def test_incomplete_pool_stop_keeps_owned_workspace_manager_open(
    tmp_path: Path,
) -> None:
    # Given
    home = tmp_path / "home"
    target = tmp_path / "other"
    home.mkdir()
    target.mkdir()
    app_config = AppConfig.model_validate({"persistence": {"backend": "sqlite"}})
    service = _service(home, app_config)
    ctx = WorkspaceContext.from_target(target, data_dir_name=".modex", home=home)
    with patch("bot.workspace.wiring.resources.PoolStore") as pool_store_type:
        pool_store_type.return_value.list_pools.return_value = []
        resources = await _build_resources(service, ctx)
    pool_instance = MagicMock()
    pool_instance.terminal_manager = None
    pool_instance.mcp_manager = None
    pool_instance.pool.shutdown_all = AsyncMock(return_value=False)
    pool_instance.broker_bridge.stop = AsyncMock()
    resources.pools["retryable"] = pool_instance
    assert resources.persistence is not None
    connection = resources.persistence.connection

    # When / Then
    with pytest.raises(RuntimeError, match="pool shutdown incomplete"):
        await _stop_resources(resources)
    assert await connection.query_value("SELECT 1", int) == 1

    pool_instance.pool.shutdown_all.return_value = True
    await _stop_resources(resources)
    with pytest.raises(ConnectionNotOpenError):
        await connection.query_value("SELECT 1", int)


async def test_cancelled_pool_stop_cleans_non_persistence_and_propagates(
    tmp_path: Path,
) -> None:
    # Given
    home = tmp_path / "home"
    target = tmp_path / "other"
    home.mkdir()
    target.mkdir()
    app_config = AppConfig.model_validate({"persistence": {"backend": "sqlite"}})
    service = _service(home, app_config)
    ctx = WorkspaceContext.from_target(target, data_dir_name=".modex", home=home)
    with patch("bot.workspace.wiring.resources.PoolStore") as pool_store_type:
        pool_store_type.return_value.list_pools.return_value = []
        resources = await _build_resources(service, ctx)
    pool_instance = MagicMock()
    pool_instance.terminal_manager = None
    pool_instance.mcp_manager = None
    pool_instance.pool.shutdown_all = AsyncMock(side_effect=asyncio.CancelledError)
    pool_instance.broker_bridge.stop = AsyncMock()
    resources.pools["cancelled"] = pool_instance
    resources.broker.stop = AsyncMock()
    assert resources.persistence is not None
    connection = resources.persistence.connection

    # When / Then
    with pytest.raises(asyncio.CancelledError):
        await _stop_resources(resources)
    pool_instance.broker_bridge.stop.assert_awaited_once()
    resources.broker.stop.assert_awaited_once()
    assert await connection.query_value("SELECT 1", int) == 1

    pool_instance.pool.shutdown_all.side_effect = None
    pool_instance.pool.shutdown_all.return_value = True
    await _stop_resources(resources)


async def test_session_registry_loads_before_pool_creation(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    app_config = AppConfig.model_validate({"persistence": {"backend": "sqlite"}})
    service = _service(home, app_config)
    manager = WorkspacePersistenceManager(home / ".modex" / "state.db")
    await manager.open()
    service._home_persistence = manager
    ctx = WorkspaceContext.from_target(home, data_dir_name=".modex", home=home)
    await manager.connection.execute(
        "INSERT INTO sessions (session_id, scope_key) VALUES (?, ?)",
        (
            "existing.main",
            '{"session_id":"existing.main","session_prefix":"existing","agent_id":"main"}',
        ),
    )
    pool_spec = MagicMock()
    pool_spec.name = "default"
    pool_spec.peers = []
    created_pool = MagicMock()
    created_pool.main_agent_name = "main"
    created_pool.pool._agents = {}
    created_pool.pool.shutdown_all = AsyncMock(return_value=True)
    created_pool.broker_bridge.start = AsyncMock()

    async def create_pool(
        *args: object,
        session_registry: SessionRegistry,
        **kwargs: object,
    ) -> MagicMock:
        loaded = await session_registry.get("existing.main")
        assert loaded is not None
        assert loaded.session_id == "existing.main"
        assert loaded.agent_name == "main"
        return created_pool

    with (
        patch("bot.workspace.wiring.resources.PoolStore") as pool_store_type,
        patch("bot.workspace.wiring.resources.build_pool_data", new=AsyncMock(return_value=MagicMock())),
        patch("bot.service.pool.create_pool", side_effect=create_pool),
        patch("bot.workspace.wiring.resources.BackgroundTaskRunner") as background_type,
    ):
        pool_store_type.return_value.list_pools.return_value = [pool_spec]
        pool_store_type.return_value.read_pool.return_value = pool_spec
        background_type.return_value.start = AsyncMock()
        background_type.return_value.stop = AsyncMock()
        resources = await _build_resources(service, ctx)

    await _stop_resources(resources)
    await manager.close()


async def test_failed_non_home_build_closes_acquired_resources(tmp_path: Path) -> None:
    home = tmp_path / "home"
    target = tmp_path / "other"
    home.mkdir()
    target.mkdir()
    app_config = AppConfig.model_validate({"persistence": {"backend": "sqlite"}})
    service = _service(home, app_config)
    ctx = WorkspaceContext.from_target(target, data_dir_name=".modex", home=home)
    manager = MagicMock(spec=WorkspacePersistenceManager)
    manager.open = AsyncMock()
    manager.close = AsyncMock()
    manager.connection = MagicMock()
    manager.connection.query_all = AsyncMock(return_value=[])
    broker = MagicMock()
    broker.start = AsyncMock()
    broker.stop = AsyncMock()

    with (
        patch("bot.workspace.wiring.resources.PoolStore") as pool_store_type,
        patch(
            "modex_agent.persistence.managers.WorkspacePersistenceManager",
            return_value=manager,
        ),
        patch("bot.workspace.wiring.resources.InMemoryMessageBroker", return_value=broker),
        patch(
            "bot.persistence.transcript.build_database_transcript_store",
            new=AsyncMock(return_value=MagicMock()),
        ),
        patch(
            "bot.workspace.wiring.pool_wiring._build_workspace_interceptor_chain",
            side_effect=RuntimeError("assembly failed"),
        ),
        pytest.raises(RuntimeError, match="assembly failed"),
    ):
        pool_store_type.return_value.list_pools.return_value = []
        await _build_resources(service, ctx)

    broker.stop.assert_awaited_once()
    manager.close.assert_awaited_once()
