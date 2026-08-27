from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from bot.service.model_choice import ModelChoiceRegistry  # noqa: E402
from bot.service.model_config import _resolved_or_placeholder  # noqa: E402
from bot.service.model_provider import BotModelProvider  # noqa: E402
from bot.tools.custom import SendFileToUserTool  # noqa: E402
from bot.webui.transcript_store import TranscriptStore  # noqa: E402
from bot.workspace.handle import WorkspaceResolverCell  # noqa: E402
from plugins.bot_hooks import (  # noqa: E402
    SEND_FILE_TO_USER_TOOL_NAME,
    BotHooksPlugin,
)
from plugins.bot_strategies import BotStrategiesPlugin  # noqa: E402

from modex_agent.multi_agent.pool_config import PoolAssemblyDeps  # noqa: E402
from modex_agent.pipeline.adapters import NullOutputAdapter  # noqa: E402
from modex_agent.plugins.abc import ComponentSlot  # noqa: E402
from modex_agent.plugins.assembly.context import (  # noqa: E402
    AssemblyContext,
    PoolContext,
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
        (ComponentSlot.TOOL, "send_file_to_user", BotHooksPlugin()),
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


def _pool_ctx(pool_assembly: MagicMock | None) -> PoolContext:
    return PoolContext(pool_runtime=PoolRuntimeDeps(pool_assembly_ctx=pool_assembly))


async def test_send_file_factory_builds_tool_from_pool_dependencies() -> None:
    assembly = MagicMock()
    assembly.assembly_deps = PoolAssemblyDeps()
    assembly.workspace_resolver = MagicMock(spec=WorkspaceResolverCell)
    sessions_dir = assembly.workspace_resolver.resolve_workspace().ctx.paths.sessions_dir
    factory = _registered(BotHooksPlugin()).resolve(
        ComponentSlot.TOOL, "send_file_to_user"
    )

    tool = await factory.create(factory.config_model(), _pool_ctx(assembly))

    assert isinstance(tool, SendFileToUserTool)
    assert tool.name == SEND_FILE_TO_USER_TOOL_NAME == "send_file_to_user"
    assert tool._output_adapter is assembly.output_adapter  # noqa: SLF001
    assert tool._transcript_store is assembly.transcript_store  # noqa: SLF001
    assert tool._media_config is assembly.assembly_deps.media  # noqa: SLF001
    assert tool._sessions_dir_provider() is sessions_dir  # noqa: SLF001


async def test_send_file_factory_missing_pool_assembly_ctx_is_actionable() -> None:
    factory = _registered(BotHooksPlugin()).resolve(
        ComponentSlot.TOOL, "send_file_to_user"
    )

    with pytest.raises(
        ValueError, match=rf"pool_assembly_ctx.*{SEND_FILE_TO_USER_TOOL_NAME}.*roster"
    ):
        await factory.create(factory.config_model(), _pool_ctx(None))


async def test_send_file_factory_missing_assembly_deps_is_actionable() -> None:
    assembly = MagicMock()
    assembly.assembly_deps = None
    factory = _registered(BotHooksPlugin()).resolve(
        ComponentSlot.TOOL, "send_file_to_user"
    )

    with pytest.raises(
        ValueError, match=rf"assembly_deps.*{SEND_FILE_TO_USER_TOOL_NAME}.*media"
    ):
        await factory.create(factory.config_model(), _pool_ctx(assembly))


async def test_send_file_factory_eval_shape_null_output_adapter_constructs() -> None:
    assembly = MagicMock()
    assembly.output_adapter = NullOutputAdapter()
    assembly.transcript_store = MagicMock(spec=TranscriptStore)
    assembly.assembly_deps = PoolAssemblyDeps()
    assembly.workspace_resolver = None
    factory = _registered(BotHooksPlugin()).resolve(
        ComponentSlot.TOOL, "send_file_to_user"
    )

    tool = await factory.create(factory.config_model(), _pool_ctx(assembly))

    assert isinstance(tool, SendFileToUserTool)
    # No workspace resolver → provider returns None (today's behavior).
    assert tool._sessions_dir_provider() is None  # noqa: SLF001
