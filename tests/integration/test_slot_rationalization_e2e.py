# allow: SIZE_OK - mandated single-file red-anchor E2E suite with shared boot harness.
"""RED-anchor E2E tests for the slot-rationalization plan (W0.2).

Four production-path E2E tests locking the TARGET behavior of waves
W2/W3/W4 in ``.omo/plans/slot-rationalization-steps.md``. Red-test anchor
discipline: no production change lands before its red anchor exists, and
each test must turn green when its wave lands — WITHOUT test edits.

==========  ==============  ===================================================
test        green at wave   locked target capability
==========  ==============  ===================================================
T-P1        W3 (3.1)        declaration ``system_prompt_provider`` names a
                            registered SYSTEM_PROMPT_PROVIDER factory; the
                            assembled subagent's system prompt contains that
                            provider's content.
T-P2        W4 (4.1/4.4)    declaration ``llm_provider`` names a registered
                            LLM_PROVIDER factory; the materialized
                            subagent's LLM provider IS that factory's
                            product instance.
T-P3        W2 (2.1)        declaration ``memory_system_config`` reaches the
                            MEMORY_SYSTEM factory (frozen config model with
                            a REQUIRED field) together with
                            ``memory_system``.
T-P4        W2 (2.2)        a memory-runner hook combined with a replaced
                            ``memory_system`` raises ``ValueError`` at
                            assembly instead of silently attaching to the
                            orphaned memory system (plan issue I2).
==========  ==============  ===================================================

Every test boots the REAL production path: hermetic scope declaration on
disk -> real boot (load + validate + compile) -> real ``create_pool`` ->
real subagent materialization, with probe plugins registered through the
real ``PluginRegistrationContext``.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar
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

from modex_agent.agents.react import ReActAgent
from modex_agent.commands.processor import SlashCommandProcessor
from modex_agent.core.constants import FinishReason
from modex_agent.core.context import ContextManager, ContextState
from modex_agent.core.llm_struct import RuntimeSafetyPolicy
from modex_agent.core.message import ChatMessage
from modex_agent.core.prompt import SystemPromptProvider
from modex_agent.core.provider import CallbackStreamProvider
from modex_agent.core.types import LLMResponse
from modex_agent.hook import HookRunner
from modex_agent.interceptor.chain import InterceptorChain
from modex_agent.memory.hooks import MemoryHook
from modex_agent.messaging.broker_memory import InMemoryMessageBroker
from modex_agent.multi_agent import SessionRetentionPolicy
from modex_agent.multi_agent.descriptor import AgentInstance
from modex_agent.multi_agent.pool_config.deps import PoolAssemblyDeps
from modex_agent.multi_agent.pool_instance import PoolInstance
from modex_agent.plugins.abc import (
    AgentType,
    ComponentFactory,
    MemoryHookFactory,
    SimpleFactory,
)
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
    from modex_agent.core.skills import SkillManager
    from modex_agent.core.tool_manager import ToolManager

pytestmark = pytest.mark.integration

_TP1_PROMPT_NAME = "probe_prompt_tp1"
_TP1_SENTINEL = "PROBE_PROMPT_ALPHA"
_TP2_LLM_NAME = "probe_llm_tp2"
_TP3_MEMORY_NAME = "probe_cm_config_tp3"
_TP3_MARKER = "XYZ789"
_TP4_MEMORY_NAME = "probe_cm_plain_tp4"
_TP4_HOOK_NAME = "probe_memory_hook_tp4"


class _EmptyConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class _ProbeMemoryConfig(BaseModel):
    """T-P3 probe config — REQUIRED marker field (W2.1 contract)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    marker: str


# ---------------------------------------------------------------------------
# Probe products
# ---------------------------------------------------------------------------


class _ProbePromptProvider(SystemPromptProvider):
    """T-P1 probe — its content is the sentinel the assembled prompt must carry."""

    async def _fetch_version(self) -> str:
        return "tp1-static"

    async def _fetch_content(self) -> str:
        return _TP1_SENTINEL


class _ProbeLLMProvider(CallbackStreamProvider):
    """T-P2 probe — identity is asserted on the materialized subagent."""

    probe_marker = _TP2_LLM_NAME

    async def chat_stream(
        self,
        messages: list[ChatMessage],
        model: str | None = None,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
        tools: list[dict] | None = None,
        on_content_delta=None,
        on_reasoning_delta=None,
        **kwargs: Any,
    ) -> LLMResponse:
        del messages, model, temperature, max_output_tokens, tools, kwargs
        return LLMResponse(content=_TP2_LLM_NAME, finish_reason=FinishReason.STOP)

    def get_default_model(self) -> str:
        return "probe-llm-model"


class _ProbeContextManagerBase(ContextManager):
    """Minimal ContextManager product for the MEMORY_SYSTEM probe factories."""

    async def load(
        self,
        session_id: str,
        runtime_info: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        tool_manager: ToolManager | None = None,
        skill_manager: SkillManager | None = None,
    ) -> ContextState:
        return ContextState(system_prompt="PROBE_MEMORY_ACTIVE")

    async def save(
        self,
        session_id: str,
        user_message: ChatMessage | dict[str, Any] | None,
        assistant_result: AgentResult,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        return None

    async def build_system_prompt(
        self,
        tool_manager: ToolManager | None,
        runtime_info: dict[str, Any] | None = None,
    ) -> str:
        return "PROBE_MEMORY_ACTIVE"

    async def clear(self, session_id: str) -> None:
        return None


class _MarkerContextManager(_ProbeContextManagerBase):
    """T-P3 probe CM — carries the marker the factory was configured with."""

    def __init__(self, marker: str) -> None:
        self.marker = marker


class _ProbeMemoryHook(MemoryHook):
    """T-P4 probe hook product (memory-runner kind; never dispatched here)."""


# ---------------------------------------------------------------------------
# Probe factories
# ---------------------------------------------------------------------------


class _ProbePromptFactory(ComponentFactory):
    config_model = _EmptyConfig

    async def create(self, config: BaseModel, ctx: AssemblyContext) -> Any:
        return _ProbePromptProvider()


class _MarkerMemoryFactory(ComponentFactory):
    """T-P3 MEMORY_SYSTEM factory — records the config it was given on the CM."""

    config_model = _ProbeMemoryConfig

    async def create(self, config: BaseModel, ctx: AssemblyContext) -> Any:
        cfg: _ProbeMemoryConfig = config  # type: ignore[assignment]
        return _MarkerContextManager(marker=cfg.marker)


class _PlainMemoryFactory(ComponentFactory):
    """T-P4 MEMORY_SYSTEM factory — empty config (gated by W2.2 only, not W2.1)."""

    config_model = _EmptyConfig

    async def create(self, config: BaseModel, ctx: AssemblyContext) -> Any:
        return _ProbeContextManagerBase()


class _ProbeMemoryHookFactory(MemoryHookFactory):
    """T-P4 HOOK factory with MEMORY hook-runner kind (orphan-hook probe)."""

    config_model: ClassVar[type[BaseModel]] = _EmptyConfig
    applies_to: ClassVar[set[AgentType] | None] = {AgentType.native_sub}

    async def create(self, config: BaseModel, ctx: AssemblyContext) -> Any:
        return _ProbeMemoryHook()


# ---------------------------------------------------------------------------
# Probe plugins (real Plugin subclasses via the real registration context)
# ---------------------------------------------------------------------------


class _PromptProbePlugin(Plugin):
    config_model = _EmptyConfig

    def register(self, ctx: PluginRegistrationContext) -> None:
        ctx.register_prompt_provider(_TP1_PROMPT_NAME, _ProbePromptFactory())


class _LLMProbePlugin(Plugin):
    config_model = _EmptyConfig

    def __init__(self, provider: _ProbeLLMProvider) -> None:
        self._provider = provider

    def register(self, ctx: PluginRegistrationContext) -> None:
        ctx.register_provider(_TP2_LLM_NAME, SimpleFactory(self._provider, _EmptyConfig))


class _MemoryConfigProbePlugin(Plugin):
    config_model = _EmptyConfig

    def register(self, ctx: PluginRegistrationContext) -> None:
        ctx.register_memory_system(_TP3_MEMORY_NAME, _MarkerMemoryFactory())


class _MemoryOrphanProbePlugin(Plugin):
    config_model = _EmptyConfig

    def register(self, ctx: PluginRegistrationContext) -> None:
        ctx.register_memory_system(_TP4_MEMORY_NAME, _PlainMemoryFactory())
        ctx.register_hook(_TP4_HOOK_NAME, _ProbeMemoryHookFactory())


# ---------------------------------------------------------------------------
# Boot harness (mirrors the reference integration suites — real production
# pool assembly, no mocking past the adapter seam)
# ---------------------------------------------------------------------------


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


def _add_probe_subagent(config_dir: Path, name: str, fields: dict) -> None:
    """Declare a probe subagent under the coder pool's root."""
    path = config_dir / "scopes" / "bot.yml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    orchestrator = raw["workspace"]["pools"]["coder"]["agents"]["orchestrator"]
    orchestrator.setdefault("agents", {})[name] = {"description": name, **fields}
    path.write_text(yaml.dump(raw, sort_keys=False, allow_unicode=True), encoding="utf-8")


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


async def _component_registry(extra_plugins: list[Plugin]) -> ComponentRegistry:
    registry = ComponentRegistry()
    await ComponentRegistryLoader.load(
        registry,
        PluginDiscoveryConfig(
            bundled_factories=[
                DefaultPlugin(),
                BotStrategiesPlugin(),
                *extra_plugins,
            ],
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


async def _materialize(
    pools: dict[str, PoolInstance],
    template_name: str,
) -> AgentInstance:
    pool = pools["coder"].pool
    template = pool.template_registry.get_template("coder", template_name)
    assert template is not None, (
        f"subagent template {template_name!r} was not seeded — the "
        "declaration field it carries was rejected"
    )
    instance = await pool.materialize_agent(
        f"inv.{template_name}", template, parent_session_id="conv.main"
    )
    assert isinstance(instance, AgentInstance)
    return instance


# ---------------------------------------------------------------------------
# T-P1 — SYSTEM_PROMPT_PROVIDER selector (green at W3)
# ---------------------------------------------------------------------------


async def test_tp1_custom_system_prompt_provider_via_roster(tmp_path: Path) -> None:
    """T-P1: a declaration-named prompt provider's content reaches the
    subagent's system prompt."""
    config_dir = _hermetic_config(tmp_path)
    _add_probe_subagent(
        config_dir,
        "tp1-prompt",
        {"system_prompt_provider": _TP1_PROMPT_NAME, "max_steps": 10},
    )
    registry = await _component_registry([_PromptProbePlugin()])
    pools, harness = await _boot_pools(config_dir, registry)
    try:
        subagent = await _materialize(pools, "tp1-prompt")
        # AgentDescriptor.system_prompt_template is the SYSTEM_PROMPT_PROVIDER
        # slot's assembly product (native_core resolves the named factory and
        # refreshes it into the descriptor).
        system_prompt = subagent.descriptor.system_prompt_template or ""
        assert _TP1_SENTINEL in system_prompt, (
            "declaration-named SYSTEM_PROMPT_PROVIDER content is missing from "
            "the assembled subagent's system prompt (_expand_system_prompt "
            "must prefer the explicit provider name over the system_prompt "
            "sugar / prompt_name / agent-name convention)"
        )
    finally:
        await harness.shutdown(pools)


# ---------------------------------------------------------------------------
# T-P2 — LLM_PROVIDER slot resolution on the sub path (green at W4)
# ---------------------------------------------------------------------------


async def test_tp2_custom_llm_provider_reaches_subagent(tmp_path: Path) -> None:
    """T-P2: the declaration-named LLM provider INSTANCE is the subagent's provider."""
    config_dir = _hermetic_config(tmp_path)
    _add_probe_subagent(
        config_dir,
        "tp2-llm",
        {"llm_provider": _TP2_LLM_NAME, "max_steps": 10},
    )
    probe_provider = _ProbeLLMProvider()
    registry = await _component_registry([_LLMProbePlugin(probe_provider)])
    pools, harness = await _boot_pools(config_dir, registry)
    try:
        subagent = await _materialize(pools, "tp2-llm")
        assert subagent.pipeline is not None
        # No public accessor exists for the agent's provider; the reference
        # suites already read pipeline internals (pipeline._turn_runner), so
        # follow the same convention: ReActAgent._llm_client._provider is the
        # provider the materialized subagent will actually call.
        agent = subagent.pipeline.agent
        assert isinstance(agent, ReActAgent)
        resolved = agent._llm_client._provider
        assert resolved is probe_provider, (
            f"materialized subagent's LLM provider is {type(resolved).__name__}, "
            "not the declaration-named probe instance (the sub path must "
            "resolve the llm_provider slot name exactly once at the "
            "production entry)"
        )
    finally:
        await harness.shutdown(pools)


# ---------------------------------------------------------------------------
# T-P3 — memory_system_config declaration face (green at W2.1)
# ---------------------------------------------------------------------------


async def test_tp3_memory_system_config_via_roster(tmp_path: Path) -> None:
    """T-P3: declaration ``memory_system_config`` reaches the MEMORY_SYSTEM factory."""
    config_dir = _hermetic_config(tmp_path)
    _add_probe_subagent(
        config_dir,
        "tp3-memory",
        {
            "memory_system": _TP3_MEMORY_NAME,
            "memory_system_config": {"marker": _TP3_MARKER},
            "max_steps": 10,
        },
    )
    registry = await _component_registry([_MemoryConfigProbePlugin()])
    pools, harness = await _boot_pools(config_dir, registry)
    try:
        subagent = await _materialize(pools, "tp3-memory")
        context_manager = subagent.context_manager
        assert isinstance(context_manager, _MarkerContextManager), (
            f"memory_system slot did not produce the probe CM: got {type(context_manager).__name__}"
        )
        # The marker could only come from the declaration
        # memory_system_config payload — this asserts the factory RECEIVED
        # the config, not {}.
        assert context_manager.marker == _TP3_MARKER, (
            "the MEMORY_SYSTEM factory never received the declaration "
            "memory_system_config payload (specs must project "
            "memory_system_config; it must never be hardcoded to {})"
        )
    finally:
        await harness.shutdown(pools)


# ---------------------------------------------------------------------------
# T-P4 — orphan memory hook must raise (green at W2.2)
# ---------------------------------------------------------------------------


async def test_tp4_memory_hook_with_replaced_memory_system_raises(tmp_path: Path) -> None:
    """T-P4: a memory-runner hook plus a replaced memory system must fail loudly."""
    config_dir = _hermetic_config(tmp_path)
    _add_probe_subagent(
        config_dir,
        "tp4-orphan",
        {
            "memory_system": _TP4_MEMORY_NAME,
            "hooks": [f"+{_TP4_HOOK_NAME}"],
            "max_steps": 10,
        },
    )
    registry = await _component_registry([_MemoryOrphanProbePlugin()])
    pools, harness = await _boot_pools(config_dir, registry)
    try:
        pool = pools["coder"].pool
        template = pool.template_registry.get_template("coder", "tp4-orphan")
        assert template is not None
        with pytest.raises(ValueError, match=_TP4_HOOK_NAME):
            await pool.materialize_agent(
                "tp4inv.tp4-orphan", template, parent_session_id="conv.main"
            )
    finally:
        await harness.shutdown(pools)
