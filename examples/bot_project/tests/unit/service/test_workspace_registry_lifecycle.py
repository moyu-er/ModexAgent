from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bot.service import BotServiceShutdownIncompleteError
from bot.service.core import BotService
from bot.workspace.wiring import WorkspaceStack

from modex_agent.ioc.configs.app import AppConfig
from modex_agent.multi_agent.pool_router import PoolRoutingStore
from modex_agent.persistence.managers import (
    RegistryPersistenceManager,
    WorkspacePersistenceManager,
)
from modex_agent.workspace.paths import RESERVED_GLOBAL_DIR, WORKSPACE_STATE_DB


async def test_initialize_closes_canonical_registry_after_materialization_failure(
    tmp_path: Path,
) -> None:
    app_config = AppConfig.model_validate(
        {
            "persistence": {"backend": "sqlite"},
            "paths": {"data_dir_name": ".state"},
        }
    )
    service = BotService.__new__(BotService)
    service._app_config = app_config
    service.config_dir = tmp_path / "config"
    service._bot_model_config = None
    service._mcp_registry = None
    service._registry_persistence = None
    service._home_persistence = None
    service.workspace_stack = None
    service._model_choice_registry = None
    service._default_provider = None
    service.control_channel = None
    service.command_processor = None
    service.plugin_integration = None
    service._pool_session_store = None

    registry = MagicMock()
    registry.initialize = AsyncMock()
    registry.materialize = AsyncMock(side_effect=RuntimeError("materialization failed"))
    registry.evict_all = AsyncMock(return_value=True)
    stack = MagicMock(spec=WorkspaceStack)
    stack.registry = registry
    stack.controller = MagicMock()

    manager = MagicMock(spec=RegistryPersistenceManager)
    manager.open = AsyncMock()
    manager.close = AsyncMock()
    home_manager = MagicMock(spec=WorkspacePersistenceManager)
    home_manager.open = AsyncMock()
    home_manager.close = AsyncMock()
    routing_store = MagicMock(spec=PoolRoutingStore)

    with (
        patch.object(
            BotService,
            "_project_dir",
            new_callable=lambda: property(lambda self: tmp_path),
        ),
        patch.object(service, "_build_default_provider", return_value=MagicMock()),
        patch.object(service, "_build_control_channel", return_value=MagicMock()),
        patch.object(service, "_build_main_command_processor", return_value=MagicMock()),
        patch("bot.service.core.PoolStore", create=True),
        patch(
            "bot.service.builders.build_pool_routing_store",
            return_value=routing_store,
            create=True,
        ) as build_routing,
        patch("bot.service.core.read_shared_registry_flag", return_value=False, create=True),
        patch(
            "modex_agent.persistence.managers.RegistryPersistenceManager",
            return_value=manager,
        ) as manager_type,
        patch(
            "modex_agent.persistence.managers.WorkspacePersistenceManager",
            return_value=home_manager,
        ) as home_manager_type,
        patch("bot.service.core.build_single_workspace_stack", return_value=stack),
        pytest.raises(RuntimeError, match="materialization failed"),
    ):
        await service.initialize()

    manager_type.assert_called_once_with(
        tmp_path / ".state" / RESERVED_GLOBAL_DIR / WORKSPACE_STATE_DB
    )
    manager.open.assert_awaited_once()
    home_manager_type.assert_called_once_with(tmp_path / ".state" / WORKSPACE_STATE_DB)
    home_manager.open.assert_awaited_once()
    build_routing.assert_called_once_with(
        app_config,
        home_manager,
        data_dir=tmp_path / ".state",
        db_path=tmp_path / ".state" / WORKSPACE_STATE_DB,
    )
    registry.initialize.assert_awaited_once()
    registry.evict_all.assert_awaited_once()
    routing_store.close.assert_called_once_with()
    home_manager.close.assert_awaited_once()
    manager.close.assert_awaited_once()
    assert service._pool_session_store is None
    assert service._home_persistence is None
    assert service._registry_persistence is None


async def test_stop_closes_shared_routing_then_home_then_registry() -> None:
    service = BotService.__new__(BotService)
    service._shutdown_event = MagicMock()
    service._maintenance_task = None
    service._router_task = None
    service.workspace_stack = MagicMock()
    service.workspace_stack.registry.evict_all = AsyncMock(return_value=True)
    service._mcp_registry = None
    service.input_adapter = MagicMock()
    service.input_adapter.stop = AsyncMock()
    closed: list[str] = []
    routing_store = MagicMock(spec=PoolRoutingStore)
    routing_store.close.side_effect = lambda: closed.append("routing")
    service._pool_session_store = routing_store
    home_manager = MagicMock(spec=WorkspacePersistenceManager)
    home_manager.close = AsyncMock(side_effect=lambda: closed.append("home"))
    service._home_persistence = home_manager
    registry_manager = MagicMock(spec=RegistryPersistenceManager)
    registry_manager.close = AsyncMock(side_effect=lambda: closed.append("registry"))
    service._registry_persistence = registry_manager

    await service.stop()

    service.workspace_stack.registry.evict_all.assert_awaited_once()
    assert closed == ["routing", "home", "registry"]
    assert service._pool_session_store is None
    assert service._home_persistence is None
    assert service._registry_persistence is None


async def test_stop_retains_shared_resources_until_eviction_retry_completes() -> None:
    # Given
    service = BotService.__new__(BotService)
    service._shutdown_event = MagicMock()
    service._maintenance_task = None
    service._router_task = None
    order: list[str] = []
    service.workspace_stack = MagicMock()
    service.workspace_stack.registry.evict_all = AsyncMock(side_effect=[False, True])
    service._mcp_registry = MagicMock()
    service._mcp_registry.shutdown = AsyncMock(side_effect=lambda: order.append("mcp"))
    service.input_adapter = MagicMock()
    service.input_adapter.stop = AsyncMock(side_effect=lambda: order.append("input"))
    routing_store = MagicMock(spec=PoolRoutingStore)
    routing_store.close.side_effect = lambda: order.append("routing")
    service._pool_session_store = routing_store
    home_manager = MagicMock(spec=WorkspacePersistenceManager)
    home_manager.close = AsyncMock(side_effect=lambda: order.append("home"))
    service._home_persistence = home_manager
    registry_manager = MagicMock(spec=RegistryPersistenceManager)
    registry_manager.close = AsyncMock(side_effect=lambda: order.append("registry"))
    service._registry_persistence = registry_manager

    # When
    with pytest.raises(BotServiceShutdownIncompleteError):
        await service.stop()

    # Then
    assert order == []
    assert service._mcp_registry is not None
    assert service._pool_session_store is routing_store
    assert service._home_persistence is home_manager
    assert service._registry_persistence is registry_manager

    # When
    await service.stop()

    # Then
    assert order == ["mcp", "input", "routing", "home", "registry"]
    assert service.workspace_stack.registry.evict_all.await_count == 2
    assert service._pool_session_store is None
    assert service._home_persistence is None
    assert service._registry_persistence is None


async def test_initialize_preserves_shared_dependencies_when_eviction_is_incomplete(
    tmp_path: Path,
) -> None:
    # Given
    app_config = AppConfig.model_validate(
        {
            "persistence": {"backend": "sqlite"},
            "paths": {"data_dir_name": ".state"},
        }
    )
    service = BotService.__new__(BotService)
    service._app_config = app_config
    service.config_dir = tmp_path / "config"
    service._bot_model_config = None
    service._mcp_registry = None
    service._registry_persistence = None
    service._home_persistence = None
    service.workspace_stack = None
    service._model_choice_registry = None
    service._default_provider = None
    service.control_channel = None
    service.command_processor = None
    service.plugin_integration = None
    service._pool_session_store = None

    initialization_error = RuntimeError("materialization failed")
    registry = MagicMock()
    registry.initialize = AsyncMock()
    registry.materialize = AsyncMock(side_effect=initialization_error)
    registry.evict_all = AsyncMock(return_value=False)
    stack = MagicMock(spec=WorkspaceStack)
    stack.registry = registry
    stack.controller = MagicMock()

    registry_manager = MagicMock(spec=RegistryPersistenceManager)
    registry_manager.open = AsyncMock()
    registry_manager.close = AsyncMock()
    home_manager = MagicMock(spec=WorkspacePersistenceManager)
    home_manager.open = AsyncMock()
    home_manager.close = AsyncMock()
    routing_store = MagicMock(spec=PoolRoutingStore)

    # When
    with (
        patch.object(
            BotService,
            "_project_dir",
            new_callable=lambda: property(lambda self: tmp_path),
        ),
        patch.object(service, "_build_default_provider", return_value=MagicMock()),
        patch.object(service, "_build_control_channel", return_value=MagicMock()),
        patch.object(service, "_build_main_command_processor", return_value=MagicMock()),
        patch("bot.service.core.PoolStore", create=True),
        patch(
            "bot.service.builders.build_pool_routing_store",
            return_value=routing_store,
            create=True,
        ),
        patch("bot.service.core.read_shared_registry_flag", return_value=False, create=True),
        patch(
            "modex_agent.persistence.managers.RegistryPersistenceManager",
            return_value=registry_manager,
        ),
        patch(
            "modex_agent.persistence.managers.WorkspacePersistenceManager",
            return_value=home_manager,
        ),
        patch("bot.service.core.build_single_workspace_stack", return_value=stack),
        pytest.raises(RuntimeError, match="materialization failed") as raised,
    ):
        await service.initialize()

    # Then
    assert raised.value is initialization_error
    assert any(
        "workspace eviction incomplete" in note for note in getattr(raised.value, "__notes__", ())
    )
    registry.evict_all.assert_awaited_once()
    routing_store.close.assert_not_called()
    home_manager.close.assert_not_awaited()
    registry_manager.close.assert_not_awaited()
    assert service._pool_session_store is routing_store
    assert service._home_persistence is home_manager
    assert service._registry_persistence is registry_manager
