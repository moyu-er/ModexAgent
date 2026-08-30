# allow: SIZE_OK - mandated single-file production E2E suite with shared boot harness.
from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest
import yaml
from pydantic import BaseModel, ConfigDict

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BOT_PROJECT = _REPO_ROOT / "examples" / "bot_project"
if str(_BOT_PROJECT) not in sys.path:
    sys.path.insert(0, str(_BOT_PROJECT))

from bot.service.pool import create_pool
from bot.service.pool.declaration import (
    boot_scope_declaration,
    declared_pool_build,
)
from plugins.bot_strategies import BotStrategiesPlugin

from modex_agent.commands.constants import CommandAction, CommandDispatchPolicy
from modex_agent.commands.handlers import CommandHandler
from modex_agent.commands.models import (
    CommandContext,
    CommandHandlingResult,
    SlashCommandInvocation,
)
from modex_agent.commands.processor import SlashCommandProcessor
from modex_agent.core.llm_struct import RuntimeSafetyPolicy
from modex_agent.core.tool_manager import Tool, ToolResult
from modex_agent.hook import HookRunner
from modex_agent.hook.abc import BeforeGraphHook
from modex_agent.interceptor.abc import ToolCallContext, ToolCallInterceptor, ToolCallNext
from modex_agent.interceptor.chain import InterceptorChain
from modex_agent.messaging.broker_memory import InMemoryMessageBroker
from modex_agent.multi_agent import SessionRetentionPolicy
from modex_agent.multi_agent.descriptor import AgentInstance
from modex_agent.multi_agent.execution_strategy import (
    ExecutionStrategy,
    PoolAssemblyContext,
    StrategyAssembly,
)
from modex_agent.multi_agent.pool_config.deps import PoolAssemblyDeps
from modex_agent.multi_agent.pool_instance import PoolInstance
from modex_agent.plugins.abc import HookRunnerKind, SimpleFactory
from modex_agent.plugins.defaults import DefaultPlugin
from modex_agent.plugins.loader import (
    ComponentRegistryLoader,
    Plugin,
    PluginDiscoveryConfig,
    PluginRegistrationContext,
)
from modex_agent.plugins.registry import (
    ComponentRegistry,
    strategy_registry_from_components,
)
from modex_agent.scope.spec import PoolSpec

if TYPE_CHECKING:
    from bot.service.pool.declaration import DeclaredPoolBuild

    from modex_agent.core.agent import AgentContext

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

_CUSTOM_TOOL_NAME = "custom_probe_tool"
_CUSTOM_HOOK_NAME = "custom_probe_hook"
_CUSTOM_STRATEGY_NAME = "my_react_probe"
_CUSTOM_INTERCEPTOR_NAME = "custom_probe_interceptor"
_CUSTOM_COMMAND_NAME = "custom_probe_command"


class _EmptyConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class _ConsumptionProbePluginConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class _CustomProbeTool(Tool):
    def __init__(self) -> None:
        super().__init__(
            name=_CUSTOM_TOOL_NAME,
            description="Returns a fixed probe result.",
            parameters={"type": "object", "properties": {}},
        )

    async def execute(self) -> ToolResult:
        return ToolResult.from_text(self.name, "probe-ok")


class _CustomProbeHook(BeforeGraphHook):
    @property
    def name(self) -> str:
        return _CUSTOM_HOOK_NAME

    async def before_graph(self, ctx: AgentContext) -> None:
        del ctx


class _CustomProbeInterceptor(ToolCallInterceptor):
    @property
    def name(self) -> str:
        return _CUSTOM_INTERCEPTOR_NAME

    async def around_tool_call(
        self,
        ctx: AgentContext,
        call: ToolCallContext,
        next_call: ToolCallNext,
    ) -> ToolResult:
        del ctx, call
        return await next_call()


class _CustomProbeCommandHandler(CommandHandler):
    @property
    def names(self) -> tuple[str, ...]:
        return (_CUSTOM_COMMAND_NAME,)

    def dispatch_policy(
        self,
        invocation: SlashCommandInvocation,
        context: CommandContext,
    ) -> CommandDispatchPolicy:
        del invocation, context
        return CommandDispatchPolicy.NORMAL_QUEUE

    async def handle(
        self,
        invocation: SlashCommandInvocation,
        context: CommandContext,
    ) -> CommandHandlingResult:
        del context
        return CommandHandlingResult(
            action=CommandAction.NOTICE,
            dispatch_policy=CommandDispatchPolicy.NORMAL_QUEUE,
            notice="custom-command-ok",
            invocation=invocation,
        )


class _ProbeAssemblyReachedError(RuntimeError):
    pass


class _ProbeExecutionStrategy(ExecutionStrategy):
    @property
    def name(self) -> str:
        return _CUSTOM_STRATEGY_NAME

    async def assemble_main(self, ctx: PoolAssemblyContext) -> StrategyAssembly:
        del ctx
        raise _ProbeAssemblyReachedError

    def validate_pool_spec(self, spec: PoolSpec) -> None:
        del spec


_CUSTOM_TOOL = _CustomProbeTool()
_CUSTOM_HOOK = _CustomProbeHook()
_CUSTOM_STRATEGY = _ProbeExecutionStrategy()
_CUSTOM_INTERCEPTOR = _CustomProbeInterceptor()
_CUSTOM_COMMAND = _CustomProbeCommandHandler()


class _ConsumptionProbePlugin(Plugin):
    config_model = _ConsumptionProbePluginConfig

    def register(self, ctx: PluginRegistrationContext) -> None:
        ctx.register_tool(
            _CUSTOM_TOOL_NAME,
            SimpleFactory(_CUSTOM_TOOL, _EmptyConfig),
        )
        hook_factory = SimpleFactory(_CUSTOM_HOOK, _EmptyConfig)
        hook_factory.applies_to = None  # type: ignore[assignment]
        hook_factory.hook_runner = HookRunnerKind.react  # type: ignore[assignment]
        ctx.register_hook(_CUSTOM_HOOK_NAME, hook_factory)
        ctx.register_execution_strategy(
            _CUSTOM_STRATEGY_NAME,
            SimpleFactory(_CUSTOM_STRATEGY, _EmptyConfig),
        )
        ctx.register_interceptor(
            _CUSTOM_INTERCEPTOR_NAME,
            SimpleFactory(_CUSTOM_INTERCEPTOR, _EmptyConfig),
        )
        ctx.register_command(
            _CUSTOM_COMMAND_NAME,
            SimpleFactory(_CUSTOM_COMMAND, _EmptyConfig),
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
        from bot.service.model_choice import ModelChoiceRegistry

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
            model_choice_registry=ModelChoiceRegistry(),
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


def _hermetic_config(tmp_path: Path) -> Path:
    """Copy the real bot config; strip the MCP selection + production-only
    hook references from the scope declaration (the hermetic registry
    bundles only the probe plugins, so those references must not survive).
    The glue tools are stripped too — experience's factory demands
    pool-layer resources (``pool_data``) and send_file_to_user's factory is
    a bot project plugin, neither of which the harness builds/loads —
    mirroring the eval overlays' glue-tool removal. The experience and todo
    capability entries are stripped for the same reason: the harness builds
    no memory system (empty ``PoolAssemblyDeps``, no ``pool_data``), and
    experience's review hook and todo's reorientation hook are
    memory-runner roster entries the capability channel contributes — they
    cannot wire without pool memory resources."""
    config_dir = tmp_path / "config"
    shutil.copytree(_BOT_PROJECT / "config", config_dir)
    declaration_path = config_dir / "scopes" / "bot.yml"
    raw = yaml.safe_load(declaration_path.read_text(encoding="utf-8"))
    workspace = raw.get("workspace", {})
    workspace.pop("mcp", None)

    _hermetic_tool_names = {"experience", "send_file_to_user"}
    _hermetic_capability_names = {"experience", "todo"}

    def strip_agents(agents: dict) -> None:
        for body in agents.values():
            if not isinstance(body, dict):
                continue
            for key in ("hooks", "hook_configs", "mcp"):
                body.pop(key, None)
            capabilities = body.get("capabilities")
            if isinstance(capabilities, dict):
                for name in _hermetic_capability_names:
                    capabilities.pop(name, None)
                if not capabilities:
                    body.pop("capabilities", None)
            tools = body.get("tools")
            if isinstance(tools, list):
                kept = [t for t in tools if str(t).lstrip("+-") not in _hermetic_tool_names]
                if kept:
                    body["tools"] = kept
                else:
                    body.pop("tools", None)
            nested = body.get("agents")
            if isinstance(nested, dict):
                strip_agents(nested)

    for pool in workspace.get("pools", {}).values():
        if isinstance(pool, dict):
            strip_agents(pool.get("agents", {}))
    declaration_path.write_text(
        yaml.dump(raw, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    return config_dir


def _edit_declaration(config_dir: Path, edit: Any) -> None:
    """Apply a surgical edit to the hermetic scope declaration."""
    path = config_dir / "scopes" / "bot.yml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    edit(raw)
    path.write_text(yaml.dump(raw, sort_keys=False, allow_unicode=True), encoding="utf-8")


def _coder_orchestrator(raw: dict) -> dict:
    return raw["workspace"]["pools"]["coder"]["agents"]["orchestrator"]


def _boot_declaration(config_dir: Path, component_registry: ComponentRegistry) -> Any:
    """The real production boot: load + validate (V1-V11) + compile."""
    from bot.service.pool.factory import _BOT_DEFAULT_LLM_PROVIDER

    return boot_scope_declaration(
        declaration_path=config_dir / "scopes" / "bot.yml",
        project_dir=config_dir.parent,
        data_dir=config_dir.parent / ".modex",
        graphs_dirs=(config_dir / "graphs",),
        default_llm_provider=_BOT_DEFAULT_LLM_PROVIDER,
        registry=component_registry,
    )


async def _component_registry() -> ComponentRegistry:
    registry = ComponentRegistry()
    await ComponentRegistryLoader.load(
        registry,
        PluginDiscoveryConfig(
            bundled_factories=(
                DefaultPlugin(),
                BotStrategiesPlugin(),
                _ConsumptionProbePlugin(),
            ),
            project_plugin_paths=(),
        ),
    )
    return registry


async def _boot_pools(
    config_dir: Path,
    registry: ComponentRegistry,
) -> tuple[dict[str, PoolInstance], _BootHarness]:
    harness = _BootHarness(config_dir, registry)
    await harness.start()
    boot = _boot_declaration(config_dir, registry)
    assert boot.spec.workspace is not None
    pools = {
        pool.name: await harness.create(pool.name, declared_pool_build(boot, pool.name))
        for pool in boot.spec.workspace.pools
    }
    return pools, harness


def _custom_component_presence(instance: AgentInstance) -> tuple[bool, bool]:
    assert instance.pipeline is not None
    assert instance.pipeline.tool_manager is not None
    assert instance.pipeline.hook_runner is not None
    has_tool = instance.pipeline.tool_manager.get_tool(_CUSTOM_TOOL_NAME) is not None
    has_hook = any(
        hook_spec.hook.name == _CUSTOM_HOOK_NAME
        for hook_spec in instance.pipeline.hook_runner.hook_specs
    )
    return has_tool, has_hook


async def test_custom_tool_and_hook_reach_materialized_subagent(tmp_path: Path) -> None:
    config_dir = _hermetic_config(tmp_path)

    def _add_probe_subagent(raw: dict) -> None:
        _coder_orchestrator(raw).setdefault("agents", {})["probe"] = {
            "description": "probe subagent",
            "tools": ["+custom_probe_tool"],
            "hooks": ["+custom_probe_hook"],
            "max_steps": 10,
        }

    _edit_declaration(config_dir, _add_probe_subagent)
    pools, harness = await _boot_pools(config_dir, await _component_registry())
    try:
        pool = pools["coder"].pool
        template = pool.template_registry.get_template("coder", "probe")
        assert template is not None
        instance = await pool.materialize_agent(
            "inv1.probe", template, parent_session_id="conv.main"
        )
        assert isinstance(instance, AgentInstance)
        assert instance.pipeline is not None
        has_tool, has_hook = _custom_component_presence(instance)
        assert has_tool and has_hook, (
            f"custom subagent components did not reach production assembly: "
            f"tool={has_tool}, hook={has_hook}"
        )
    finally:
        await harness.shutdown(pools)


async def test_custom_tool_and_hook_reach_main_agent(tmp_path: Path) -> None:
    """Declaration path: the root's ``tools``/``hooks`` rosters are
    first-class declaration fields consumed by Stage 4 assembly."""
    config_dir = _hermetic_config(tmp_path)

    def _add_main_components(raw: dict) -> None:
        orchestrator = _coder_orchestrator(raw)
        orchestrator["tools"] = ["+custom_probe_tool"]
        orchestrator["hooks"] = ["+custom_probe_hook"]

    _edit_declaration(config_dir, _add_main_components)
    pools, harness = await _boot_pools(config_dir, await _component_registry())
    try:
        coder = pools["coder"]
        main = coder.pool._agents.get(coder.root_agent_name)
        assert isinstance(main, AgentInstance)
        has_tool, has_hook = _custom_component_presence(main)
        assert has_tool and has_hook, (
            f"custom main components did not reach production assembly: "
            f"tool={has_tool}, hook={has_hook}"
        )
    finally:
        await harness.shutdown(pools)


async def test_custom_execution_strategy_plugin_passes_gating(tmp_path: Path) -> None:
    """A custom strategy name flows honestly from the declaration to gating."""
    config_dir = _hermetic_config(tmp_path)

    def _set_probe_strategy(raw: dict) -> None:
        _coder_orchestrator(raw)["execution_strategy"] = _CUSTOM_STRATEGY_NAME

    _edit_declaration(config_dir, _set_probe_strategy)
    registry = await _component_registry()
    boot = _boot_declaration(config_dir, registry)
    declared = declared_pool_build(boot, "coder")
    harness = _BootHarness(config_dir, registry)
    await harness.start()
    pools: dict[str, PoolInstance] = {}
    try:
        with pytest.raises(_ProbeAssemblyReachedError):
            await harness.create("coder", declared)
    finally:
        await harness.shutdown(pools)


async def test_baseline_materialize_without_custom_refs_still_works(
    tmp_path: Path,
) -> None:
    config_dir = _hermetic_config(tmp_path)

    def _add_baseline_subagent(raw: dict) -> None:
        _coder_orchestrator(raw).setdefault("agents", {})["baseline"] = {
            "description": "baseline subagent",
            "max_steps": 10,
        }

    _edit_declaration(config_dir, _add_baseline_subagent)
    pools, harness = await _boot_pools(config_dir, await _component_registry())
    try:
        pool = pools["coder"].pool
        template = pool.template_registry.get_template("coder", "baseline")
        assert template is not None
        instance = await pool.materialize_agent(
            "inv2.baseline", template, parent_session_id="conv.main"
        )
        assert isinstance(instance, AgentInstance)
        assert instance.pipeline is not None
    finally:
        await harness.shutdown(pools)


async def test_pool_interceptor_and_command_rosters_are_consumed_in_isolation(
    tmp_path: Path,
) -> None:
    config_dir = _hermetic_config(tmp_path)

    def _add_rosters(raw: dict) -> None:
        orchestrator = _coder_orchestrator(raw)
        orchestrator["interceptors"] = [_CUSTOM_INTERCEPTOR_NAME]
        orchestrator["commands"] = [_CUSTOM_COMMAND_NAME]

    _edit_declaration(config_dir, _add_rosters)
    pools, harness = await _boot_pools(config_dir, await _component_registry())
    try:
        coder = pools["coder"].pool._agents[pools["coder"].root_agent_name]
        default = pools["default"].pool._agents[pools["default"].root_agent_name]
        assert coder.pipeline is not None
        assert default.pipeline is not None
        coder_chain = coder.pipeline._turn_runner.interceptor_chain
        default_chain = default.pipeline._turn_runner.interceptor_chain
        assert coder_chain is not None
        assert default_chain is not None
        assert _CUSTOM_INTERCEPTOR in coder_chain.interceptors
        assert _CUSTOM_INTERCEPTOR not in default_chain.interceptors

        processor = coder.pipeline.command_processor
        assert processor is not None
        result = await processor.handle(
            f"/{_CUSTOM_COMMAND_NAME}",
            CommandContext(
                session_id="conv.coder",
                input_msg=MagicMock(),
                agent_name=pools["coder"].root_agent_name,
            ),
        )
        assert result.notice == "custom-command-ok"
    finally:
        await harness.shutdown(pools)
