from __future__ import annotations

from pathlib import Path

import pytest
from bot.service import builders
from bot.service.pool.declaration import (
    boot_scope_declaration,
    declared_pool_build,
)
from bot.service.pool.factory import _BOT_DEFAULT_LLM_PROVIDER

from modex_agent.plugins.defaults import DefaultPlugin
from modex_agent.plugins.loader import ComponentRegistryLoader, PluginDiscoveryConfig
from modex_agent.plugins.registry import ComponentRegistry

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
