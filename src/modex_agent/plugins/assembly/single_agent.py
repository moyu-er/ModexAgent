"""Poolless assembly for one compiled root agent."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from modex_agent.core.emitter import ContentEmitter
from modex_agent.core.llm_struct import RuntimeSafetyPolicy
from modex_agent.core.prompt import SystemPromptProvider
from modex_agent.core.provider import LLMProvider
from modex_agent.core.scope import MemoryAgentRole
from modex_agent.core.tool_manager import InMemoryToolManager, Tool
from modex_agent.hook import Hook
from modex_agent.ioc.factories.governance import create_governance
from modex_agent.ioc.factories.memory import create_memory
from modex_agent.memory.core.system import MemorySystem
from modex_agent.memory.system import MemorySystemContextManager
from modex_agent.multi_agent.descriptor import AgentDescriptor, AgentInstance
from modex_agent.multi_agent.factory import DefaultAgentFactory
from modex_agent.plugins.abc import ComponentSlot
from modex_agent.plugins.assembly.context import (
    AssemblyContext,
    PoolRuntimeDeps,
    agent_context_chain,
    resolution_context,
)
from modex_agent.plugins.assembly.native_core import (
    LlmDefaults,
    NativeAssemblyInputs,
    _resolve_single,
    assemble_native_agent,
)
from modex_agent.plugins.registry import ComponentRegistry
from modex_agent.scope.compiler import CompiledAgent
from modex_agent.scope.defaults import memory_config_for_position
from modex_agent.scope.derivation import _DEFAULT_LLM_PROVIDER
from modex_agent.tools.workspace_scoped import WorkspaceRootProvider
from modex_agent.workspace.context import WorkspaceContext
from modex_agent.workspace.paths import WorkspacePaths


class SingleAgentInfra:
    """Live infrastructure supplied to standalone assembly."""

    def __init__(
        self,
        llm_provider: LLMProvider | None,
        safety: RuntimeSafetyPolicy,
        root_provider: WorkspaceRootProvider | None,
        *,
        tool_wrapper: Callable[[Tool], Tool] | None = None,
        extra_hooks: tuple[Hook, ...] = (),
        governance_enabled: bool = True,
        emitter_factory: Callable[[str], ContentEmitter[Any]] | None = None,
    ) -> None:
        self.llm_provider = llm_provider
        self.safety = safety
        self.root_provider = root_provider
        self.tool_wrapper = tool_wrapper
        self.extra_hooks = extra_hooks
        self.governance_enabled = governance_enabled
        self.emitter_factory = emitter_factory


class SingleAgentAssembled:
    """Standalone runtime and the handles needed by evaluation harnesses."""

    def __init__(
        self,
        *,
        instance: AgentInstance,
        memory_system: MemorySystem,
        context_manager: MemorySystemContextManager,
        tool_manager: InMemoryToolManager,
        descriptor: AgentDescriptor,
    ) -> None:
        self.instance = instance
        self.memory_system = memory_system
        self.context_manager = context_manager
        self.tool_manager = tool_manager
        self.descriptor = descriptor


async def _resolve_provider(
    compiled: CompiledAgent,
    infra: SingleAgentInfra,
    component_registry: ComponentRegistry,
    component_ctx: AssemblyContext,
) -> LLMProvider:
    spec = compiled.spec
    if infra.llm_provider is not None and spec.llm_provider == _DEFAULT_LLM_PROVIDER:
        return infra.llm_provider
    chain = agent_context_chain(component_ctx, spec=spec)
    return await _resolve_single(
        component_registry,
        ComponentSlot.LLM_PROVIDER,
        spec.llm_provider,
        spec.llm_provider_config,
        chain,
    )


async def _resolve_prompt(
    compiled: CompiledAgent,
    component_registry: ComponentRegistry,
    component_ctx: AssemblyContext,
    project_dir: Path,
) -> str:
    spec = compiled.spec
    config = dict(spec.system_prompt_config)
    if "path" in config:
        prompt_path = Path(config["path"])
        if not prompt_path.is_absolute():
            config["path"] = str(project_dir / prompt_path)
    chain = agent_context_chain(component_ctx, spec=spec)
    provider: SystemPromptProvider = await _resolve_single(
        component_registry,
        ComponentSlot.SYSTEM_PROMPT_PROVIDER,
        spec.system_prompt_provider,
        config,
        chain,
    )
    return await provider.get_or_refresh()


async def assemble_declared_single_agent(
    compiled: CompiledAgent,
    infra: SingleAgentInfra,
    *,
    project_dir: Path,
    data_dir: Path,
    component_registry: ComponentRegistry,
) -> SingleAgentAssembled:
    """Assemble one compiled root without a pool, bus, inbox, or poller."""
    spec = compiled.spec
    workspace_ctx = WorkspaceContext(
        target=project_dir,
        paths=WorkspacePaths(root=data_dir),
        is_home=False,
    )
    component_ctx = resolution_context(
        component_registry,
        workspace_ctx,
        PoolRuntimeDeps(
            root_provider=infra.root_provider,
            emitter_factory=infra.emitter_factory,
        ),
    )
    provider = await _resolve_provider(compiled, infra, component_registry, component_ctx)
    memory_config = memory_config_for_position(
        compiled.defaults,
        session_max_context_tokens=spec.memory_overrides.max_context_tokens,
    )
    memory_dir = data_dir / "memory" / spec.pool_name
    memory_dir.mkdir(parents=True, exist_ok=True)
    memory_system = create_memory(memory_config, provider, memory_dir)
    await memory_system.initialize()
    context_manager = MemorySystemContextManager(
        memory_system=memory_system,
        default_agent_id=spec.agent_name,
        default_agent_role=MemoryAgentRole.MAIN,
        base_system_prompt=await _resolve_prompt(
            compiled, component_registry, component_ctx, project_dir
        ),
        roles=list(spec.roles),
    )
    tool_manager = InMemoryToolManager()
    result = await assemble_native_agent(
        spec,
        component_registry,
        NativeAssemblyInputs(
            agent_factory=DefaultAgentFactory(default_llm_provider=provider),
            broker=None,
            llm_defaults=LlmDefaults(),
            pool=None,
            context_manager=context_manager,
            memory_system=memory_system,
            memory_config=memory_config,
            llm_provider=provider,
            tool_manager=tool_manager,
            root_provider=infra.root_provider,
            safety=infra.safety,
            project_dir=project_dir,
            extra_hooks=infra.extra_hooks,
            # The shared native registration seam transforms roster, synthesized
            # companion, and MCP tools before the factory receives the manager.
            # A wrapping manager cannot do this honestly because companion
            # detection must inspect the unwrapped PersistentBashTool.
            tool_transform=infra.tool_wrapper,
        ),
        ctx=component_ctx,
    )
    governance = create_governance(memory_config) if infra.governance_enabled else None
    if result.instance.pipeline is not None:
        builder = result.instance.pipeline._turn_context_builder
        if builder is not None:
            builder.governance = governance
        if infra.emitter_factory is not None:
            result.instance.pipeline._turn_runner.set_emitter_factory(infra.emitter_factory)
    return SingleAgentAssembled(
        instance=result.instance,
        memory_system=memory_system,
        context_manager=context_manager,
        tool_manager=result.tool_manager,
        descriptor=result.descriptor,
    )
