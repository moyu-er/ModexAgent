from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from bot.service.model_choice import ModelChoiceRegistry  # noqa: E402
from bot.service.model_config import _resolved_or_placeholder  # noqa: E402
from bot.service.model_provider import BotModelProvider  # noqa: E402
from plugins.bot_hooks import BotHooksPlugin  # noqa: E402
from plugins.bot_strategies import BotStrategiesPlugin  # noqa: E402

from modex_agent.memory.tools.experience import ExperienceTool  # noqa: E402
from modex_agent.pipeline.snapshot import PoolDataSnapshot  # noqa: E402
from modex_agent.plugins.abc import ComponentSlot  # noqa: E402
from modex_agent.plugins.assembly.context import (  # noqa: E402
    AssemblyContext,
    PoolRuntimeDeps,
)
from modex_agent.plugins.loader import PluginRegistrationContext  # noqa: E402
from modex_agent.plugins.registry import ComponentRegistry  # noqa: E402
from modex_agent.tools.terminal import TerminalManagerBase  # noqa: E402


def _registered(plugin: BotStrategiesPlugin | BotHooksPlugin) -> ComponentRegistry:
    registry = ComponentRegistry()
    with PluginRegistrationContext(registry) as registration:
        plugin.register(registration)
    return registry


def _ctx(
    *,
    pool_assembly: MagicMock | None = None,
    terminal_manager: TerminalManagerBase | None = None,
) -> AssemblyContext:
    return AssemblyContext(
        registry=MagicMock(),
        workspace_ctx=MagicMock(),
        pool_runtime=PoolRuntimeDeps(
            pool_assembly_ctx=pool_assembly,
            terminal_manager=terminal_manager,
        ),
    )


def _pool_assembly() -> MagicMock:
    assembly = MagicMock()
    assembly.pool_spec.main.use_terminal = True
    assembly.pool_spec.main.terminal_visibility = False
    assembly.bot_model_config = _resolved_or_placeholder(None)
    assembly.model_choice_registry = MagicMock(spec=ModelChoiceRegistry)
    return assembly


@pytest.mark.parametrize(
    ("slot", "name", "plugin"),
    [
        (ComponentSlot.LLM_PROVIDER, "bot_default", BotStrategiesPlugin()),
        (ComponentSlot.TOOL, "experience", BotHooksPlugin()),
    ],
)
def test_bot_runtime_factory_registration_and_config_contract(
    slot: ComponentSlot,
    name: str,
    plugin: BotStrategiesPlugin | BotHooksPlugin,
) -> None:
    factory = _registered(plugin).resolve(slot, name)

    assert factory.config_model.model_config.get("frozen") is True
    assert factory.config_model.model_config.get("extra") == "forbid"
    assert factory.config_model.model_fields == {}


async def test_bot_default_factory_uses_pool_model_dependencies() -> None:
    assembly = _pool_assembly()
    factory = _registered(BotStrategiesPlugin()).resolve(
        ComponentSlot.LLM_PROVIDER, "bot_default"
    )

    provider = await factory.create(factory.config_model(), _ctx(pool_assembly=assembly))

    assert isinstance(provider, BotModelProvider)
    assert provider._model_config is assembly.bot_model_config  # noqa: SLF001


@pytest.mark.parametrize("missing", ["bot_model_config", "model_choice_registry"])
async def test_bot_default_factory_missing_dependency_is_actionable(missing: str) -> None:
    assembly = _pool_assembly()
    setattr(assembly, missing, None)
    factory = _registered(BotStrategiesPlugin()).resolve(
        ComponentSlot.LLM_PROVIDER, "bot_default"
    )

    with pytest.raises(ValueError, match=rf"{missing}.*roster"):
        await factory.create(factory.config_model(), _ctx(pool_assembly=assembly))


async def test_experience_factory_uses_pool_data_directory(tmp_path: Path) -> None:
    assembly = _pool_assembly()
    assembly.pool_data = MagicMock(spec=PoolDataSnapshot)
    assembly.pool_data.experience_dir = tmp_path / "experiences"
    factory = _registered(BotHooksPlugin()).resolve(ComponentSlot.TOOL, "experience")

    tool = await factory.create(factory.config_model(), _ctx(pool_assembly=assembly))

    assert isinstance(tool, ExperienceTool)
    assert tool.name == "experience"
    assert assembly.pool_data.experience_dir.is_dir()


@pytest.mark.parametrize("missing", ["pool_data", "experience_dir"])
async def test_experience_factory_missing_dependency_is_actionable(missing: str) -> None:
    assembly = _pool_assembly()
    if missing == "pool_data":
        assembly.pool_data = None
    else:
        assembly.pool_data = MagicMock(spec=PoolDataSnapshot)
        assembly.pool_data.experience_dir = None
    factory = _registered(BotHooksPlugin()).resolve(ComponentSlot.TOOL, "experience")

    with pytest.raises(ValueError, match=rf"{missing}.*roster"):
        await factory.create(factory.config_model(), _ctx(pool_assembly=assembly))
