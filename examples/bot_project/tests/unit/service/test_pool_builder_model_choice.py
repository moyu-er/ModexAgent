from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parents[3]))

from bot.service.model_choice import ModelChoiceBindHook, ModelChoiceRegistry
from bot.service.model_config import BotModelConfig
from bot.service.model_provider import BotModelProvider
from bot.service.pool.factory import _resolve_llm_slot

from modex_agent.core.llm_struct import RuntimeSafetyPolicy
from modex_agent.multi_agent import SessionRetentionPolicy
from modex_agent.multi_agent.execution_strategy import PoolAssemblyContext
from modex_agent.pipeline.turn_session_registry import TurnSessionRegistry
from modex_agent.plugins.loader import ComponentRegistryLoader, PluginDiscoveryConfig
from modex_agent.plugins.registry import ComponentRegistry
from modex_agent.scope.spec import AgentSpec, PoolSpec
from modex_agent.workspace.context import WorkspaceContext
from modex_agent.workspace.paths import WorkspacePaths

pytestmark = pytest.mark.skipif(
    shutil.which("modexctl") is None,
    reason="modexctl CLI not available",
)

_YML = """
models:
  default_provider: "A"
  default_model: "M1"
  providers:
    - {key: a, name: "A", url: u, api_key: k, models: [{name: M1, model: openai/m1}]}
"""


def _cfg(tmp_path: Path) -> BotModelConfig:
    p = tmp_path / "model.yml"
    p.write_text(_YML, encoding="utf-8")
    return BotModelConfig.from_yaml(p)


class _Agent:
    name = "main"

    async def run(self, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401  test stub matches Agent ABC loosely
        ...


@pytest.mark.asyncio
async def test_default_config_resolves_bot_model_provider(tmp_path: Path) -> None:
    """W4.2 behavior preservation: with no roster override the pool's slot
    name is ``bot_default`` and its resolved product is a BotModelProvider —
    the same class the main agent has always run on."""
    from plugins.bot_strategies import BotStrategiesPlugin

    cfg = _cfg(tmp_path)
    registry = ComponentRegistry()
    await ComponentRegistryLoader.load(
        registry,
        PluginDiscoveryConfig(
            bundled_factories=(BotStrategiesPlugin(),),
            project_plugin_paths=(),
        ),
    )
    pool_spec = PoolSpec(name="main", agents=[AgentSpec(name="main")])
    pool_assembly_ctx = PoolAssemblyContext(
        pool_name="main",
        pool_spec=pool_spec,
        project_dir=tmp_path,
        data_dir=tmp_path / ".modex",
        broker=MagicMock(),
        inbox_server=MagicMock(),
        agent_bus=MagicMock(),
        output_adapter=MagicMock(),
        safety=RuntimeSafetyPolicy(),
        retention=SessionRetentionPolicy(),
        registry=TurnSessionRegistry(),
        bot_model_config=cfg,
        model_choice_registry=ModelChoiceRegistry(),
    )
    ws_ctx = WorkspaceContext(target=tmp_path, paths=WorkspacePaths(root=tmp_path), is_home=False)

    provider = await _resolve_llm_slot(registry, "bot_default", {}, pool_assembly_ctx, ws_ctx)

    assert isinstance(provider, BotModelProvider)


async def test_wire_main_pipeline_adds_model_choice_hook(tmp_path: Path) -> None:
    """The model-choice binding is a declared roster entry (``hooks:
    [+model_choice_bind]`` in bot.yml) dispatched at Stage 4 since the W6
    glue eradication — ``_wire_main_pipeline`` no longer injects it. The
    factory path pins the bot-side contract: the hook derives
    ``BotModelConfig`` + ``ModelChoiceRegistry`` from the pool assembly
    context the create_pool road threads."""
    from plugins.bot_hooks import ModelChoiceBindHookFactory

    from modex_agent.plugins.abc import AgentType
    from modex_agent.plugins.assembly.context import (
        PoolRuntimeDeps,
        agent_context_chain,
        resolution_context,
    )
    from modex_agent.plugins.assembly.spec import AssemblySpec, MemoryOverrides

    cfg = _cfg(tmp_path)
    reg = ModelChoiceRegistry()
    pool_spec = PoolSpec(name="main", agents=[AgentSpec(name="main")])
    pool_assembly_ctx = PoolAssemblyContext(
        pool_name="main",
        pool_spec=pool_spec,
        project_dir=tmp_path,
        data_dir=tmp_path / ".modex",
        broker=MagicMock(),
        inbox_server=MagicMock(),
        agent_bus=MagicMock(),
        output_adapter=MagicMock(),
        safety=RuntimeSafetyPolicy(),
        retention=SessionRetentionPolicy(),
        registry=TurnSessionRegistry(),
        bot_model_config=cfg,
        model_choice_registry=reg,
    )
    ws_ctx = WorkspaceContext(target=tmp_path, paths=WorkspacePaths(root=tmp_path), is_home=False)
    spec = AssemblySpec(
        agent_type=AgentType.native_main,
        agent_name="main",
        pool_name="main",
        tools=[],
        hooks=["model_choice_bind"],
        llm_provider="bot_default",
        system_prompt_provider="file_prompt",
        system_prompt_config={},
        memory_overrides=MemoryOverrides(),
        execution_strategy="react",
        workspace_ctx=ws_ctx,
    )
    component_ctx = resolution_context(
        MagicMock(), ws_ctx, PoolRuntimeDeps(pool_assembly_ctx=pool_assembly_ctx)
    )
    chain = agent_context_chain(component_ctx, spec=spec)

    hook = await ModelChoiceBindHookFactory().create(
        ModelChoiceBindHookFactory.config_model(), chain
    )
    assert isinstance(hook, ModelChoiceBindHook)
    assert hook._model_config is cfg  # noqa: SLF001 — pin the threaded config identity
    assert hook._registry is reg  # noqa: SLF001
