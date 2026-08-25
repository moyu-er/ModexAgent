from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Final
from unittest.mock import MagicMock

import pytest
import yaml
from pydantic import BaseModel, ConfigDict

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BOT_PROJECT = _REPO_ROOT / "examples" / "bot_project"
if str(_BOT_PROJECT) not in sys.path:
    sys.path.insert(0, str(_BOT_PROJECT))

from bot.service.pool import create_pool
from plugins.bot_strategies import BotStrategiesPlugin

from modex_agent.commands.processor import SlashCommandProcessor
from modex_agent.core.context import ContextManager, ContextState
from modex_agent.core.llm_struct import RuntimeSafetyPolicy
from modex_agent.hook import HookRunner
from modex_agent.interceptor.chain import InterceptorChain
from modex_agent.messaging.broker_memory import InMemoryMessageBroker
from modex_agent.multi_agent import SessionRetentionPolicy
from modex_agent.multi_agent.descriptor import AgentInstance
from modex_agent.multi_agent.pool_config.deps import PoolAssemblyDeps
from modex_agent.multi_agent.pool_instance import PoolInstance
from modex_agent.plugins.abc import ComponentFactory
from modex_agent.plugins.assembly.context import AssemblyContext
from modex_agent.plugins.defaults import DefaultPlugin
from modex_agent.plugins.loader import (
    ComponentRegistryLoader,
    Plugin,
    PluginDiscoveryConfig,
    PluginRegistrationContext,
)
from modex_agent.plugins.registry import ComponentRegistry, strategy_registry_from_components

if TYPE_CHECKING:
    from bot.service.pool.declaration import DeclaredPoolBuild

    from modex_agent.core.emitter import AgentResult
    from modex_agent.core.message import ChatMessage
    from modex_agent.core.skills import SkillManager
    from modex_agent.core.tool_manager import ToolManager

def _modexctl_resolvable() -> bool:
    """Mirror the production resolution (env override > venv sibling > PATH).

    ``shutil.which`` alone would skip machines where modexctl is installed
    next to the interpreter (wheel layout) but not on PATH.
    """
    try:
        from modex_agent.agents.external.cli_resolver import resolve_modexctl_bin_dir

        resolve_modexctl_bin_dir()
    except Exception:
        return False
    return True


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _modexctl_resolvable(), reason="modexctl binary not resolvable"
    ),
]

_PROBE_MEMORY_NAME: Final = "probe_memory"
_PROBE_SYSTEM_PROMPT: Final = "PROBE_MEMORY_ACTIVE"


class _ProbeMemoryConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class _ProbeContextManager(ContextManager):
    async def load(
        self,
        session_id: str,
        runtime_info: dict[str, object] | None = None,
        metadata: dict[str, object] | None = None,
        tool_manager: ToolManager | None = None,
        skill_manager: SkillManager | None = None,
    ) -> ContextState:
        return ContextState(system_prompt=_PROBE_SYSTEM_PROMPT)

    async def save(
        self,
        session_id: str,
        user_message: ChatMessage | dict[str, object] | None,
        assistant_result: AgentResult,
        metadata: dict[str, object] | None = None,
    ) -> None:
        return None

    async def build_system_prompt(
        self,
        tool_manager: ToolManager | None,
        runtime_info: dict[str, object] | None = None,
    ) -> str:
        return _PROBE_SYSTEM_PROMPT

    async def clear(self, session_id: str) -> None:
        return None


class _ProbeMemoryFactory(ComponentFactory):
    config_model = _ProbeMemoryConfig

    async def create(
        self,
        config: BaseModel,
        ctx: AssemblyContext,
    ) -> _ProbeContextManager:
        return _ProbeContextManager()


class _ProbeMemoryPlugin(Plugin):
    config_model = _ProbeMemoryConfig

    def register(self, ctx: PluginRegistrationContext) -> None:
        ctx.register_memory_system(
            _PROBE_MEMORY_NAME,
            _ProbeMemoryFactory(),
        )


class _BootHarness:
    def __init__(self, config_dir: Path, component_registry: ComponentRegistry) -> None:
        self.config_dir = config_dir
        self.component_registry = component_registry
        self.broker = InMemoryMessageBroker()
        self.strategy_registry = strategy_registry_from_components(component_registry)

    async def start(self) -> None:
        await self.broker.start()

    async def create(self, pool_name: str, declared: DeclaredPoolBuild) -> PoolInstance:
        return await create_pool(
            pool_name=pool_name,
            declared=declared,
            assembly_deps=PoolAssemblyDeps(),
            project_dir=self.config_dir.parent,
            data_dir=self.config_dir.parent / ".modex",
            broker=self.broker,
            output_adapter=MagicMock(),
            safety=RuntimeSafetyPolicy(),
            retention=SessionRetentionPolicy(),
            im_ui=MagicMock(),
            shared_hooks=[],
            shared_hook_runner=HookRunner(),
            shared_interceptor_chain=InterceptorChain(),
            bot_model_config=None,
            model_choice_registry=MagicMock(),
            app_config=None,
            strategy_registry=self.strategy_registry,
            workspace_registry=object(),
            workspace_resources=object(),
            component_registry=self.component_registry,
            command_processor=SlashCommandProcessor.default(),
        )

    async def shutdown(self, pools: dict[str, PoolInstance]) -> None:
        for instance in pools.values():
            await instance.pool.shutdown_all()
        await self.broker.stop()


def _strip_hermetic_fields(raw: dict) -> None:
    """Drop MCP selections + production-only hook references from the copied
    declaration (the hermetic registry bundles only the probe plugins)."""
    workspace = raw.get("workspace")
    if isinstance(workspace, dict):
        workspace.pop("mcp", None)

    def strip_agents(agents: dict) -> None:
        for body in agents.values():
            if not isinstance(body, dict):
                continue
            for key in ("hooks", "hook_configs", "mcp"):
                body.pop(key, None)
            nested = body.get("agents")
            if isinstance(nested, dict):
                strip_agents(nested)

    pools = workspace.get("pools", {}) if isinstance(workspace, dict) else {}
    for pool in pools.values():
        if isinstance(pool, dict):
            strip_agents(pool.get("agents", {}))


def _hermetic_config(tmp_path: Path) -> Path:
    config_dir = tmp_path / "config"
    shutil.copytree(_BOT_PROJECT / "config", config_dir)
    declaration_path = config_dir / "scopes" / "bot.yml"
    raw = yaml.safe_load(declaration_path.read_text(encoding="utf-8"))
    _strip_hermetic_fields(raw)
    # The probe memory system replaces the coder main's context manager AND
    # a declared subagent's (the two MEMORY_SYSTEM consumption surfaces).
    orchestrator = raw["workspace"]["pools"]["coder"]["agents"]["orchestrator"]
    orchestrator["memory_system"] = _PROBE_MEMORY_NAME
    orchestrator.setdefault("agents", {})["probe-memory"] = {
        "description": "probe-memory subagent",
        "memory_system": _PROBE_MEMORY_NAME,
        "max_steps": 10,
    }
    declaration_path.write_text(
        yaml.dump(raw, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    return config_dir


async def _component_registry() -> ComponentRegistry:
    registry = ComponentRegistry()
    await ComponentRegistryLoader.load(
        registry,
        PluginDiscoveryConfig(
            bundled_factories=(
                DefaultPlugin(),
                BotStrategiesPlugin(),
                _ProbeMemoryPlugin(),
            ),
            project_plugin_paths=(),
        ),
    )
    return registry


def _boot_declaration(config_dir: Path):
    """The real production boot: load + validate (V1-V11) + compile."""
    from bot.service.pool.declaration import boot_scope_declaration
    from bot.service.pool.factory import _BOT_DEFAULT_LLM_PROVIDER

    return boot_scope_declaration(
        declaration_path=config_dir / "scopes" / "bot.yml",
        project_dir=config_dir.parent,
        data_dir=config_dir.parent / ".modex",
        graphs_dirs=(config_dir / "graphs",),
        default_llm_provider=_BOT_DEFAULT_LLM_PROVIDER,
    )


async def _boot_pools(
    config_dir: Path,
    registry: ComponentRegistry,
) -> tuple[dict[str, PoolInstance], _BootHarness]:
    from bot.service.pool.declaration import declared_pool_build

    harness = _BootHarness(config_dir, registry)
    await harness.start()
    boot = _boot_declaration(config_dir)
    assert boot.spec.workspace is not None
    pools = {
        pool.name: await harness.create(pool.name, declared_pool_build(boot, pool.name))
        for pool in boot.spec.workspace.pools
    }
    return pools, harness


async def test_memory_system_plugin_replaces_main_and_subagent_context_manager(
    tmp_path: Path,
) -> None:
    config_dir = _hermetic_config(tmp_path)
    pools, harness = await _boot_pools(config_dir, await _component_registry())
    try:
        coder = pools["coder"]
        main_agent = coder.pool._agents.get(coder.root_agent_name)
        assert isinstance(main_agent, AgentInstance)
        assert isinstance(main_agent.context_manager, _ProbeContextManager)
        main_state = await main_agent.context_manager.load("main-session")
        assert main_state.system_prompt == _PROBE_SYSTEM_PROMPT

        template = coder.pool.template_registry.get_template("coder", "probe-memory")
        assert template is not None
        subagent = await coder.pool.materialize_agent(
            "inv1.probe-memory",
            template,
            parent_session_id="conv.main",
        )
        assert isinstance(subagent.context_manager, _ProbeContextManager)
        subagent_state = await subagent.context_manager.load("subagent-session")
        assert subagent_state.system_prompt == _PROBE_SYSTEM_PROMPT
    finally:
        await harness.shutdown(pools)
