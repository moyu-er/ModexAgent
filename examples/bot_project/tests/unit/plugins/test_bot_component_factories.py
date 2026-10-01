from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from bot.tools.custom import SendFileToUserTool  # noqa: E402
from bot.webui.transcript_store import TranscriptStore  # noqa: E402
from bot.workspace.handle import WorkspaceResolverCell  # noqa: E402
from bot_plugins.bot_hooks import (  # noqa: E402
    SEND_FILE_TO_USER_TOOL_NAME,
    BotHooksPlugin,
)

from modex_agent.adapters.output import NullOutputAdapter  # noqa: E402
from modex_agent.multi_agent.pool_config import PoolAssemblyDeps  # noqa: E402
from modex_agent.plugins.assembly.context import (  # noqa: E402
    PoolContext,
    PoolRuntimeDeps,
)
from modex_agent.plugins.loader import PluginRegistrationContext  # noqa: E402
from modex_agent.scope.component_registry import ComponentRegistry  # noqa: E402
from modex_agent.scope.components import ComponentSlot  # noqa: E402


def _registered(plugin: BotHooksPlugin) -> ComponentRegistry:
    registry = ComponentRegistry()
    with PluginRegistrationContext(registry) as registration:
        plugin.register(registration)
    return registry


def test_send_file_factory_config_contract() -> None:
    factory = _registered(BotHooksPlugin()).resolve(
        ComponentSlot.TOOL, "send_file_to_user"
    )

    assert factory.config_model.model_config.get("frozen") is True
    assert factory.config_model.model_config.get("extra") == "forbid"
    assert factory.config_model.model_fields == {}


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
