from __future__ import annotations

from pathlib import Path

import pytest
from bot.service import builders
from bot.service.pool.declaration import (
    DeclaredPoolBuild,
    boot_scope_declaration,
    declared_pool_build,
)
from bot.service.pool.factory import _BOT_DEFAULT_LLM_PROVIDER

from modex_agent.plugins.defaults import DefaultPlugin
from modex_agent.plugins.loader import ComponentRegistryLoader, PluginDiscoveryConfig
from modex_agent.plugins.registry import ComponentRegistry
from tests.eval.harbor.test_convergence_characterization import (
    PRODUCTION_ORDERED_TOOLS,
    PRODUCTION_POOL_PROMPT,
)

_BOT_PROJECT = Path(__file__).resolve().parents[3]


async def _registry() -> ComponentRegistry:
    registry = ComponentRegistry()
    await ComponentRegistryLoader.load(
        registry,
        PluginDiscoveryConfig(
            bundled_factories=(DefaultPlugin(),),
            project_plugin_paths=(_BOT_PROJECT / "plugins",),
        ),
    )
    return registry


def _declared(
    declaration_path: Path, project_dir: Path, data_dir: Path
) -> DeclaredPoolBuild:
    boot = boot_scope_declaration(
        declaration_path=declaration_path,
        project_dir=project_dir,
        data_dir=data_dir,
        graphs_dirs=(_BOT_PROJECT / "config" / "graphs",),
        default_llm_provider=_BOT_DEFAULT_LLM_PROVIDER,
    )
    return declared_pool_build(boot, "default")


@pytest.mark.asyncio
async def test_production_root_prompt_and_roster_match_frozen_pins(tmp_path: Path) -> None:
    # Given
    declared = _declared(
        _BOT_PROJECT / "config" / "scopes" / "bot.yml",
        _BOT_PROJECT,
        tmp_path / ".modex",
    )
    registry = await _registry()

    # When
    prompt = await builders.resolve_declared_root_prompt(declared, _BOT_PROJECT, registry)

    # Then
    assert prompt == PRODUCTION_POOL_PROMPT
    assert tuple(declared.root.effective.tools) == PRODUCTION_ORDERED_TOOLS


@pytest.mark.asyncio
async def test_declared_file_prompt_returns_empty_for_missing_file(tmp_path: Path) -> None:
    # Given
    declaration = tmp_path / "scope.yml"
    declaration.write_text(
        """
pool:
  name: default
  agents:
    default:
      system_prompt: agents/missing.md
""",
        encoding="utf-8",
    )
    boot = boot_scope_declaration(
        declaration_path=declaration,
        project_dir=tmp_path,
        data_dir=tmp_path / ".modex",
        graphs_dirs=(tmp_path / "no-graphs",),
        default_llm_provider=_BOT_DEFAULT_LLM_PROVIDER,
    )
    declared = declared_pool_build(boot, "default")
    registry = await _registry()

    # When
    prompt = await builders.resolve_declared_root_prompt(declared, tmp_path, registry)

    # Then
    assert prompt == ""
