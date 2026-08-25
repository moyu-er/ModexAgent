from __future__ import annotations

from dataclasses import fields
from pathlib import Path
from unittest.mock import MagicMock

from bot.workspace.handle import PoolWorkspaceResources
from bot.workspace.wiring.resources import _build_resources, _stop_resources

from modex_agent.ioc.configs.app import AppConfig
from modex_agent.multi_agent.pool_router import PoolRoutingStore
from modex_agent.persistence.config import PersistenceBackend
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
    return service


async def _build_empty_workspace(
    tmp_path: Path,
    backend: PersistenceBackend,
) -> PoolWorkspaceResources:
    home = tmp_path / "home"
    target = tmp_path / "workspace"
    home.mkdir()
    target.mkdir()
    app_config = AppConfig.model_validate({"persistence": {"backend": backend}})
    service = _service(home, app_config)
    ctx = WorkspaceContext.from_target(target, data_dir_name=".modex", home=home)

    scopes_dir = home / "config" / "scopes"
    scopes_dir.mkdir(parents=True, exist_ok=True)
    (scopes_dir / "bot.yml").write_text(
        "workspace:\n  name: empty\n  pools: {}\n", encoding="utf-8"
    )
    return await _build_resources(service, ctx)


def test_pool_workspace_resources_declares_kb_provider_field() -> None:
    # Given / When
    field_names = {resource_field.name for resource_field in fields(PoolWorkspaceResources)}

    # Then
    assert "kb_provider" in field_names


async def test_kb_provider_is_built_for_sqlite_workspace(tmp_path: Path) -> None:
    # Given / When
    resources = await _build_empty_workspace(tmp_path, PersistenceBackend.SQLITE)

    # Then
    try:
        assert resources.kb_provider is not None
    finally:
        await _stop_resources(resources)


async def test_kb_provider_is_none_for_file_workspace(tmp_path: Path) -> None:
    # Given / When
    resources = await _build_empty_workspace(tmp_path, PersistenceBackend.FILE)

    # Then
    try:
        assert resources.kb_provider is None
    finally:
        await _stop_resources(resources)


async def test_kb_provider_is_built_after_workspace_transcript_store(
    tmp_path: Path,
) -> None:
    # Given / When
    resources = await _build_empty_workspace(tmp_path, PersistenceBackend.SQLITE)

    # Then
    try:
        assert resources.workspace_transcript_store is not None
        assert resources.kb_provider is not None
    finally:
        await _stop_resources(resources)
