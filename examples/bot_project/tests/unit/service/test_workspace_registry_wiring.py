from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import MagicMock

from bot.workspace.wiring import build_workspace_stack

from modex_agent.app.config import AppConfig
from modex_agent.messaging.models import InputMessage
from modex_agent.persistence.managers import RegistryPersistenceManager
from modex_agent.plugins.assembly.backend_factory import build_workspace_registry_store
from modex_agent.workspace.registry import ScopeRegistryStore
from modex_agent.workspace.store import GlobalWorkspaceStore


def _config(backend: str) -> AppConfig:
    return AppConfig.model_validate({"persistence": {"backend": backend}})


async def _receive() -> AsyncIterator[InputMessage]:
    if False:
        yield InputMessage.model_construct()


async def test_builder_selects_sqlite_registry_store(tmp_path: Path) -> None:
    persistence = RegistryPersistenceManager(tmp_path / "state.db")
    await persistence.open()
    try:
        store = build_workspace_registry_store(
            _config("sqlite"), persistence, tmp_path, ".modex"
        )
        assert store is persistence.store
        assert isinstance(store, ScopeRegistryStore)
    finally:
        await persistence.close()


def test_builder_selects_file_registry_store(tmp_path: Path) -> None:
    store = build_workspace_registry_store(
        _config("file"), None, tmp_path, ".modex"
    )
    assert isinstance(store, GlobalWorkspaceStore)


def test_workspace_stack_uses_configured_sqlite_store(tmp_path: Path) -> None:
    persistence = RegistryPersistenceManager(tmp_path / "state.db")
    service = MagicMock()
    service._project_dir = tmp_path
    service._app_config = _config("sqlite")
    service._registry_persistence = persistence
    service.input_adapter.receive = _receive

    stack = build_workspace_stack(service, data_dir_name=".modex")

    assert stack.store is persistence.store


def test_workspace_stack_uses_file_store_without_registry_manager(tmp_path: Path) -> None:
    service = MagicMock()
    service._project_dir = tmp_path
    service._app_config = _config("file")
    service._registry_persistence = None
    service.input_adapter.receive = _receive

    stack = build_workspace_stack(service, data_dir_name=".modex")

    assert isinstance(stack.store, GlobalWorkspaceStore)
