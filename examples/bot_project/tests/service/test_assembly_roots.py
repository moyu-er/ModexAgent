"""T02 — BotAssemblyRoots: explicit config/resource/workspace roots.

Covers the acp-adapter DESIGN §3.2 roots split at the bot assembly seam:

- config root (``config_dir``) owns app/model/scope/MCP config files;
- resource root owns bundled/project plugins, graph templates and
  declaration-referenced resources (today's ``BotService._project_dir``);
- workspace home owns runtime data (data dir, registry/home DBs, pool
  routing store, ScopeRegistry home).

Resident defaults must resolve to exactly today's paths, item by item.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import bot.service.core as bot_service_core
import pytest
from bot.service.core import BotService
from bot.service.pool.declaration import boot_scope_declaration
from bot.service.pool.factory import _BOT_DEFAULT_LLM_PROVIDER
from bot.service.roots import BotAssemblyRoots
from bot.workspace.handle import WorkspaceHandle, WorkspaceHandleRootProvider
from bot.workspace.wiring import build_workspace_stack

from modex_agent.workspace.paths import RESERVED_GLOBAL_DIR, WORKSPACE_STATE_DB

PROJECT_DIR = Path(bot_service_core.__file__).resolve().parent.parent.parent
CONFIG_DIR = PROJECT_DIR / "config"


def _make_service(
    roots: BotAssemblyRoots | None = None,
    *,
    config_dir: Path | None = None,
    enable_dynamic_workspaces: bool = True,
) -> BotService:
    return BotService(
        config_dir=config_dir or CONFIG_DIR,
        input_adapter=MagicMock(),
        output_adapter=MagicMock(),
        emitter_factory=lambda session_id, pool_name: MagicMock(),
        app_config=None,
        roots=roots,
        enable_dynamic_workspaces=enable_dynamic_workspaces,
    )


def test_default_roots_are_resident_equivalent() -> None:
    """No roots supplied → resident identity: every root is today's path."""
    service = _make_service()
    assert service.roots == BotAssemblyRoots.resident(
        config_dir=CONFIG_DIR,
        resource_root=PROJECT_DIR,
    )
    assert service.roots.workspace_home == PROJECT_DIR


def test_default_roots_match_legacy_layout() -> None:
    """Resident derived paths equal the pre-roots literals, item by item."""
    roots = _make_service().roots
    assert roots.scope_declaration_path == PROJECT_DIR / "config" / "scopes" / "bot.yml"
    assert roots.mcp_registry_path == PROJECT_DIR / "config" / "mcp" / "registry.json"
    assert roots.plugins_dir == PROJECT_DIR / "plugins"
    assert roots.graphs_dir == PROJECT_DIR / "config" / "graphs"
    assert roots.home_data_dir("data") == PROJECT_DIR / "data"
    assert roots.home_db_path("data") == PROJECT_DIR / "data" / WORKSPACE_STATE_DB
    assert roots.registry_db_path("data") == (
        PROJECT_DIR / "data" / RESERVED_GLOBAL_DIR / WORKSPACE_STATE_DB
    )


def test_resident_custom_config_dir_keeps_legacy_declaration_source(
    tmp_path: Path,
) -> None:
    """roots=None: a custom --config directory moves app/model config only —
    the scope declaration + MCP registry stay at the install-root config,
    the historical hardcoded source (characterization of resident defaults)."""
    service = _make_service(config_dir=tmp_path / "custom-config")
    assert service.roots.config_dir == tmp_path / "custom-config"
    assert service.roots.scope_declaration_path == (
        PROJECT_DIR / "config" / "scopes" / "bot.yml"
    )
    assert service.roots.mcp_registry_path == (
        PROJECT_DIR / "config" / "mcp" / "registry.json"
    )


def test_explicit_roots_split_config_and_workspace(tmp_path: Path) -> None:
    """Explicit roots: scope/MCP under config root, data under workspace home."""
    workspace_home = tmp_path / "ide-project"
    roots = BotAssemblyRoots(
        config_dir=CONFIG_DIR,
        resource_root=PROJECT_DIR,
        workspace_home=workspace_home,
    )
    assert roots.scope_declaration_path == CONFIG_DIR / "scopes" / "bot.yml"
    assert roots.mcp_registry_path == CONFIG_DIR / "mcp" / "registry.json"
    assert roots.plugins_dir == PROJECT_DIR / "plugins"
    assert roots.graphs_dir == PROJECT_DIR / "config" / "graphs"
    assert roots.home_data_dir("data") == workspace_home / "data"
    assert roots.home_db_path("data") == workspace_home / "data" / WORKSPACE_STATE_DB
    assert roots.registry_db_path("data") == (
        workspace_home / "data" / RESERVED_GLOBAL_DIR / WORKSPACE_STATE_DB
    )


def test_roots_config_dir_mismatch_raises_loudly(tmp_path: Path) -> None:
    """Roots whose config root disagrees with the positional config_dir fail."""
    with pytest.raises(ValueError, match="config_dir"):
        _make_service(
            BotAssemblyRoots(
                config_dir=tmp_path / "elsewhere",
                resource_root=PROJECT_DIR,
                workspace_home=tmp_path / "ide-project",
            )
        )


def test_enable_dynamic_workspaces_flag_is_carried() -> None:
    service = _make_service(enable_dynamic_workspaces=False)
    assert service._enable_dynamic_workspaces is False
    assert _make_service()._enable_dynamic_workspaces is True


@pytest.mark.asyncio
async def test_explicit_roots_bind_registry_home_to_workspace_home(
    tmp_path: Path,
) -> None:
    """The workspace stack's registry home is the RUNTIME workspace root."""
    workspace_home = tmp_path / "ide-project"
    service = _make_service(
        BotAssemblyRoots(
            config_dir=CONFIG_DIR,
            resource_root=PROJECT_DIR,
            workspace_home=workspace_home,
        )
    )
    stack = build_workspace_stack(service, data_dir_name="data", enabled=False)
    assert stack.registry.home == workspace_home
    assert stack.registry.home_context.target == workspace_home
    assert stack.registry.home_context.is_home is True
    # enabled=False (single-project assembly): the /cd entry refuses.
    result = await stack.controller.open_workspace(str(PROJECT_DIR))
    assert result.success is False


def test_compiled_workspace_ctx_keeps_resource_base_and_workspace_data(
    tmp_path: Path,
) -> None:
    """Scope-boot compile: asset base stays at the resource root (bot assets
    must resolve for a bot-config-less IDE project) while the data side of
    the workspace pair roots at the workspace data dir."""
    from modex_agent.plugins.defaults import DefaultPlugin
    from modex_agent.plugins.loader import PluginRegistrationContext
    from modex_agent.plugins.registry import ComponentRegistry

    registry = ComponentRegistry()
    ctx = PluginRegistrationContext(registry)
    DefaultPlugin().register(ctx)
    ctx.flush()

    workspace_data = tmp_path / "ide-project" / ".modex"
    boot = boot_scope_declaration(
        declaration_path=CONFIG_DIR / "scopes" / "bot.yml",
        project_dir=PROJECT_DIR,
        data_dir=workspace_data,
        graphs_dirs=(PROJECT_DIR / "config" / "graphs",),
        default_llm_provider=_BOT_DEFAULT_LLM_PROVIDER,
        registry=registry,
    )
    assert boot.compilation.agents
    for agent in boot.compilation.agents:
        assert agent.spec.workspace_ctx.target == PROJECT_DIR
        assert agent.spec.workspace_ctx.paths.root == workspace_data


def test_workspace_handle_roots_tools_at_workspace_target(tmp_path: Path) -> None:
    """The tool/terminal/sandbox root provider reads the WORKSPACE target —
    the IDE runtime root under explicit roots — never the resource root."""
    workspace_home = tmp_path / "ide-project"
    handle = WorkspaceHandle(target=workspace_home, data_root=tmp_path / "data")
    assert WorkspaceHandleRootProvider(handle).current() == workspace_home
    assert WorkspaceHandle(target=PROJECT_DIR, data_root=tmp_path / "d").current == PROJECT_DIR
