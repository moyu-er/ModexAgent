"""ExperienceToolFactory — the EXPERIENCE supplement's FW tool factory.

Moved from ``examples/bot_project/plugins/bot_hooks.py`` (behavior
verbatim): ``create()`` resolves the pool's experience directory through
the context chain (``pool_runtime.pool_assembly_ctx.pool_data``),
materializes it, and wraps it with the per-file metadata store. Missing
pool-layer supply fails loudly (a roster-referenced component is never
silently skipped) — same pattern as ``TestExperienceReviewChainSupply``
in ``test_defaults_hooks.py``.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from modex_agent.plugins.abc import ComponentSlot
from modex_agent.plugins.assembly.context import PoolContext, PoolRuntimeDeps
from modex_agent.plugins.defaults.tools import (
    ExperienceToolFactory,
    register_default_tools,
)
from modex_agent.plugins.loader import PluginRegistrationContext
from modex_agent.plugins.registry import ComponentRegistry
from modex_agent.tools.presets import ToolSupplement, get_supplement_tool_names

_EXPERIENCE_NAME = get_supplement_tool_names([ToolSupplement.EXPERIENCE])[0]


def _register_defaults() -> ComponentRegistry:
    registry = ComponentRegistry()
    with PluginRegistrationContext(registry) as ctx:
        register_default_tools(ctx)
    return registry


def _ctx(pool_runtime: PoolRuntimeDeps | None) -> PoolContext:
    return PoolContext(pool_runtime=pool_runtime)


def _pool_runtime(pool_data: object | None) -> PoolRuntimeDeps:
    pool_assembly = MagicMock()
    pool_assembly.pool_data = pool_data
    return PoolRuntimeDeps(pool_assembly_ctx=pool_assembly)


def _pool_data(experience_dir: object | None) -> object:
    pool_data = MagicMock()
    pool_data.experience_dir = experience_dir
    return pool_data


class TestExperienceToolRegistration:
    """The EXPERIENCE supplement's projected name resolves in the TOOL slot."""

    def test_experience_factory_registered_under_projected_name(self) -> None:
        registry = _register_defaults()
        factory = registry.resolve(ComponentSlot.TOOL, _EXPERIENCE_NAME)
        assert isinstance(factory, ExperienceToolFactory)


class TestExperienceToolChainSupply:
    """create() assembles the tool from pool-layer supply, fail-loud."""

    async def test_creates_tool_from_pool_data(self, tmp_path) -> None:
        from modex_agent.memory.tools.experience import ExperienceTool

        experience_dir = tmp_path / "experiences"
        pool_runtime = _pool_runtime(_pool_data(experience_dir))
        factory = ExperienceToolFactory()
        tool = await factory.create(factory.config_model(), _ctx(pool_runtime))
        assert isinstance(tool, ExperienceTool)
        assert tool.name == _EXPERIENCE_NAME
        assert experience_dir.exists()

    async def test_missing_pool_assembly_ctx_raises_loud(self) -> None:
        factory = ExperienceToolFactory()
        with pytest.raises(ValueError, match="pool_assembly_ctx"):
            await factory.create(factory.config_model(), _ctx(PoolRuntimeDeps()))

    async def test_missing_pool_data_raises_loud(self) -> None:
        factory = ExperienceToolFactory()
        with pytest.raises(ValueError, match="pool_data"):
            await factory.create(factory.config_model(), _ctx(_pool_runtime(None)))

    async def test_missing_experience_dir_raises_loud(self) -> None:
        factory = ExperienceToolFactory()
        with pytest.raises(ValueError, match="experience_dir"):
            await factory.create(
                factory.config_model(), _ctx(_pool_runtime(_pool_data(None)))
            )
