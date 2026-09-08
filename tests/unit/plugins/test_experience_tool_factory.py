"""ExperienceToolFactory — the ``experience`` capability's tool factory.

``create()`` resolves the pool's ``experience`` capability supply through
the context chain (``capability_supply['experience']`` — the supply owns
the catalog) and builds the router tool on it. Missing supply fails
loudly — same pattern as the dark-supply pins in
``test_experience_supply.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from modex_agent.plugins.abc import ComponentSlot
from modex_agent.plugins.assembly.context import PoolContext, PoolRuntimeDeps
from modex_agent.plugins.capability import PoolSupplyAgentEntry, PoolSupplyView
from modex_agent.plugins.defaults.capabilities.experience import (
    EXPERIENCE_TOOL_NAME,
    ExperienceCapability,
    ExperienceSupply,
)
from modex_agent.plugins.defaults.capabilities.experience.registration import (
    register_experience_feature,
)
from modex_agent.plugins.defaults.capabilities.experience.tool_factory import (
    ExperienceToolFactory,
)
from modex_agent.plugins.loader import PluginRegistrationContext
from modex_agent.plugins.registry import ComponentRegistry

_EXPERIENCE_NAME = EXPERIENCE_TOOL_NAME


def _register_defaults() -> ComponentRegistry:
    registry = ComponentRegistry()
    with PluginRegistrationContext(registry) as ctx:
        register_experience_feature(ctx)
    return registry


def _ctx(pool_runtime: PoolRuntimeDeps | None) -> PoolContext:
    return PoolContext(pool_runtime=pool_runtime)


def _supply(data_dir: Path) -> ExperienceSupply:
    supply = ExperienceCapability().supply(
        PoolSupplyView(
            pool_name="p",
            entries=(PoolSupplyAgentEntry(agent_name="main", config={}),),
            root_agent_name="main",
            data_dir=data_dir,
        )
    )
    assert isinstance(supply, ExperienceSupply)
    return supply


class TestExperienceToolRegistration:
    """The experience capability's contributed tool name resolves in the
    TOOL slot (through the package's single registration entry)."""

    def test_experience_factory_registered_under_contributed_name(self) -> None:
        registry = _register_defaults()
        factory = registry.resolve(ComponentSlot.TOOL, _EXPERIENCE_NAME)
        assert isinstance(factory, ExperienceToolFactory)


class TestExperienceToolChainSupply:
    """create() assembles the tool from the capability supply, fail-loud."""

    async def test_creates_tool_from_capability_supply(self, tmp_path: Path) -> None:
        from modex_agent.plugins.defaults.capabilities.experience.catalog import (
            ExperienceRouterTool,
        )

        supply = _supply(tmp_path)
        pool_runtime = PoolRuntimeDeps(capability_supply={"experience": supply})
        factory = ExperienceToolFactory()
        tool = await factory.create(factory.config_model(), _ctx(pool_runtime))
        assert isinstance(tool, ExperienceRouterTool)
        assert tool.name == _EXPERIENCE_NAME
        assert supply.experience_dir.exists()

    async def test_missing_pool_runtime_raises_loud(self) -> None:
        factory = ExperienceToolFactory()
        with pytest.raises(ValueError, match="experience"):
            await factory.create(factory.config_model(), _ctx(None))

    async def test_missing_supply_key_raises_loud(self) -> None:
        """The bare-tool degraded mode (``tools: [+experience]`` without
        the capability): the tool name rides the roster but no supply
        exists — the factory loud-fails with the repair path."""
        factory = ExperienceToolFactory()
        with pytest.raises(ValueError, match=r"capabilities: \{experience: \{\}\}"):
            await factory.create(factory.config_model(), _ctx(PoolRuntimeDeps()))

    async def test_wrong_supply_type_raises_loud(self, tmp_path: Path) -> None:
        from tests.unit.plugins.test_experience_supply import _WrongSupply

        pool_runtime = PoolRuntimeDeps(capability_supply={"experience": _WrongSupply()})
        factory = ExperienceToolFactory()
        with pytest.raises(ValueError, match="ExperienceSupply"):
            await factory.create(factory.config_model(), _ctx(pool_runtime))
