"""Shared native-agent component resolution and runtime construction."""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, TypeVar

from pydantic import BaseModel, ConfigDict

from modex_agent.core.agent import ExecutionStrategyKind
from modex_agent.core.capabilities import ModelInfo
from modex_agent.core.llm_request import ReasoningEffort
from modex_agent.core.prompt import SystemPromptProvider
from modex_agent.core.tool_manager import Tool
from modex_agent.hook import Hook, HookSpec
from modex_agent.hook.runner import HookRunner
from modex_agent.ioc.configs.memory import ArchiveConfig, CoreMemoryConfig, MemoryConfig
from modex_agent.memory.core.system import MemorySystem
from modex_agent.memory.presets import subagent_memory
from modex_agent.memory.system import MemorySystemContextManager
from modex_agent.multi_agent.address import AgentAddress
from modex_agent.multi_agent.comm_kind import AgentCommKind
from modex_agent.multi_agent.descriptor import (
    AgentDescriptor,
    AgentInstance,
    AgentLLMConfig,
    ContextStrategy,
)
from modex_agent.plugins.abc import AgentType, ComponentSlot, HookRunnerKind
from modex_agent.plugins.assembly.context import (
    AgentContext,
    AssemblyContext,
    agent_context_chain,
)
from modex_agent.plugins.assembly.spec import AssemblySpec, MemoryOverrides
from modex_agent.plugins.capability import CapabilityWiring
from modex_agent.tools.manager import InMemoryToolManager

if TYPE_CHECKING:
    from modex_agent.adapters.output import OutputAdapter
    from modex_agent.commands.skill import SkillResolver
    from modex_agent.core.llm_struct import RuntimeSafetyPolicy
    from modex_agent.core.provider import LLMProvider
    from modex_agent.memory.context import ContextManager
    from modex_agent.messaging import MessageBroker
    from modex_agent.multi_agent.factory import AgentFactory
    from modex_agent.multi_agent.pool import AgentPool
    from modex_agent.plugins.registry import ComponentRegistry
    from modex_agent.tools.workspace_scoped import WorkspaceRootProvider


T = TypeVar("T")

logger = logging.getLogger(__name__)


class LlmDefaults(BaseModel):
    """Descriptor-level model defaults retained by the runtime factory."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model: str | None = None
    temperature: float = 0.7
    max_output_tokens: int | None = None
    reasoning_effort: ReasoningEffort = ReasoningEffort.NONE
    model_info: ModelInfo | None = None


class NativeAssemblyInputs:
    """Typed dependencies shared by native main and subagent construction.

    Only fields consumed by :func:`assemble_native_agent` live here. Pool-scoped
    runtime objects (tree, resolver, todo store, MCP registry, emitter factory,
    …) flow through ``PoolRuntimeDeps`` on the ``AssemblyContext`` where hook
    and tool factories read them — not through this carrier.
    """

    def __init__(
        self,
        agent_factory: AgentFactory,
        broker: MessageBroker | None,
        llm_defaults: LlmDefaults,
        *,
        pool: AgentPool | None = None,
        context_manager: ContextManager | None = None,
        memory_system: MemorySystem | None = None,
        memory_config: MemoryConfig | None = None,
        llm_provider: LLMProvider | None = None,
        tool_manager: InMemoryToolManager | None = None,
        skill_resolver: SkillResolver | None = None,
        output_adapter: OutputAdapter | None = None,
        root_provider: WorkspaceRootProvider | None = None,
        safety: RuntimeSafetyPolicy | None = None,
        project_dir: Path | None = None,
        on_subagent_created: Callable[[str, str], Awaitable[None]] | None = None,
        extra_hooks: tuple[Hook, ...] = (),
        execution_strategy: ExecutionStrategyKind = ExecutionStrategyKind.REACT,
        tool_transform: Callable[[Tool], Tool] | None = None,
    ) -> None:
        self.agent_factory = agent_factory
        self.broker = broker
        self.llm_defaults = llm_defaults
        self.pool = pool
        self.context_manager = context_manager
        self.memory_system = memory_system
        self.memory_config = memory_config
        self.llm_provider = llm_provider
        self.tool_manager = tool_manager
        self.skill_resolver = skill_resolver
        self.output_adapter = output_adapter
        self.root_provider = root_provider
        self.safety = safety
        self.project_dir = project_dir
        self.on_subagent_created = on_subagent_created
        self.extra_hooks = extra_hooks
        self.execution_strategy = execution_strategy
        self.tool_transform = tool_transform


class NativeAssemblyResult:
    """Authoritative native runtime plus its resolved component products."""

    def __init__(
        self,
        *,
        descriptor: AgentDescriptor,
        instance: AgentInstance,
        tool_manager: InMemoryToolManager,
        llm_provider: LLMProvider,
        system_prompt_provider: SystemPromptProvider,
        system_prompt: str,
        memory_config: MemoryConfig,
        hook_runner: HookRunner,
        mcp_backend: Any | None = None,
        capability_wirings: Mapping[str, CapabilityWiring] | None = None,
    ) -> None:
        self.descriptor = descriptor
        self.instance = instance
        self.tool_manager = tool_manager
        self.llm_provider = llm_provider
        self.system_prompt_provider = system_prompt_provider
        self.system_prompt = system_prompt
        self.memory_config = memory_config
        self.hook_runner = hook_runner
        self.mcp_backend = mcp_backend
        # Per-capability wiring products keyed by registration name; None
        # when constructed outside the capability dispatch (direct tests).
        self.capability_wirings = capability_wirings


async def _resolve_multi(
    registry: ComponentRegistry,
    slot: ComponentSlot,
    names: list[str],
    configs: Mapping[str, Mapping[str, object]],
    ctx: AgentContext,
) -> list[T]:
    instances: list[T] = []
    for name in names:
        factory = registry.resolve(slot, name)
        config = factory.config_model.model_validate(configs.get(name, {}))
        instance: T = await factory.create(config, ctx)
        instances.append(instance)
    return instances


async def _resolve_single(
    registry: ComponentRegistry,
    slot: ComponentSlot,
    name: str,
    config_data: Mapping[str, object],
    ctx: AgentContext,
) -> T:
    factory = registry.resolve(slot, name)
    config = factory.config_model.model_validate(config_data)
    instance: T = await factory.create(config, ctx)
    return instance


def _merge_memory(
    fallback: MemoryConfig | None,
    overrides: MemoryOverrides,
) -> MemoryConfig:
    base: MemoryConfig = fallback if fallback is not None else subagent_memory()
    if overrides.max_context_tokens is not None:
        session = base.session.model_copy(
            update={"max_context_tokens": overrides.max_context_tokens}
        )
        base = base.model_copy(update={"session": session})
    if overrides.archive_enabled is not None:
        if overrides.archive_enabled:
            # M1: toggle enabled only — the preset's archive internals
            # (max_entries, scope, inject tuning, ...) survive. Fresh
            # construction only when the base layer is absent.
            archive = (
                base.archive.model_copy(update={"enabled": True})
                if base.archive is not None
                else ArchiveConfig(enabled=True)
            )
        else:
            archive = None
        base = base.model_copy(update={"archive": archive})
    if overrides.core_enabled is not None:
        if overrides.core_enabled:
            core = (
                base.core.model_copy(update={"enabled": True})
                if base.core is not None
                else CoreMemoryConfig(enabled=True)
            )
        else:
            core = None
        base = base.model_copy(update={"core": core})
    return base


async def _dispatch_hooks(
    spec: AssemblySpec,
    registry: ComponentRegistry,
    ctx: AgentContext,
    hook_runner: HookRunner,
    memory_system: MemorySystem | None,
) -> None:
    for name in spec.hooks:
        factory = registry.resolve(ComponentSlot.HOOK, name)
        if factory.applies_to is not None and spec.agent_type not in factory.applies_to:
            continue
        config = factory.config_model.model_validate(spec.hook_configs.get(name, {}))
        hook = await factory.create(config, ctx)
        match factory.hook_runner:
            case HookRunnerKind.react:
                # The factory's declared priority rides the HookSpec —
                # HookRunner sorts by it (negative = runs first).
                hook_runner.add(HookSpec(hook=hook, priority=factory.priority))
            case HookRunnerKind.memory:
                if spec.memory_system is not None:
                    raise ValueError(
                        f"Memory hook {name!r} cannot be wired: the roster "
                        f"memory system {spec.memory_system!r} replaces the "
                        "framework memory system this hook would attach to. "
                        "Register the hook inside the custom ContextManager "
                        "instead."
                    )
                if memory_system is None:
                    raise ValueError(f"Memory hook {name!r} requires a memory system")
                memory_system.add_cleanup_hook(hook)
            case _:
                raise ValueError(
                    f"HOOK factory {name!r} ({type(factory).__name__}) declares "
                    "neither a react nor a memory hook runner. Subclass "
                    "ReactHookFactory or MemoryHookFactory so the dispatched "
                    "hook gets a runner."
                )


async def assemble_native_agent(
    spec: AssemblySpec,
    registry: ComponentRegistry,
    inputs: NativeAssemblyInputs,
    *,
    ctx: AssemblyContext,
    parent_session: str | None = None,
    invocation_id: str | None = None,
) -> NativeAssemblyResult:
    """Resolve native components once, create the runtime, and register it."""
    # Ticket 04: factories receive the per-agent full-chain context —
    # the legacy AssemblyContext view is lifted into the layered chain
    # at this boundary (both views coexist until the W3 tickets).
    chain = agent_context_chain(
        ctx,
        spec=spec,
        parent_session=parent_session,
        invocation_id=invocation_id,
        llm_defaults=inputs.llm_defaults,
    )
    # Capability dispatch (SPEC §7.2) runs BEFORE tool resolution: the
    # per-agent wiring artifacts (e.g. the ``subagents`` capability's
    # per-agent communication target store) must be ON the chain for the
    # TOOL-slot factories resolving the roster below. The merged prompt
    # providers feed the capability-section anchor of the native memory
    # context manager later in this function (SPEC §7.3) — the sections
    # must be set before the first load(), which happens at runtime
    # after this assembly returns.
    capability_wirings: dict[str, CapabilityWiring] = {}
    for compiled_cap in spec.capabilities:
        cap = registry.resolve_capability(compiled_cap.name)
        wiring = await cap.assemble(compiled_cap.binding, chain)
        capability_wirings[compiled_cap.name] = wiring
    if capability_wirings:
        chain = dataclasses.replace(chain, capability_wirings=MappingProxyType(capability_wirings))
    tools: list[Tool] = await _resolve_multi(
        registry, ComponentSlot.TOOL, spec.tools, spec.tool_configs, chain
    )
    # GENERIC fallback for direct callers: the production main path (create_pool
    # resolves the slot via _resolve_llm_slot) and the production sub path (the
    # pool factory pre-resolves into AgentMaterializeDeps) both always pass
    # inputs.llm_provider, so this registry resolution is production-dead by
    # design — it exists so assemble_native_agent remains usable standalone
    # (tests, tooling) without a pre-resolved provider.
    provider = inputs.llm_provider
    if provider is None:
        provider = await _resolve_single(
            registry,
            ComponentSlot.LLM_PROVIDER,
            spec.llm_provider,
            spec.llm_provider_config,
            chain,
        )
    # Resolve MEMORY_SYSTEM slot if spec references it (SPEC Errata-7).
    if spec.memory_system is not None:
        from dataclasses import replace as _replace

        chain_with_provider = _replace(chain, llm_provider=provider)
        mem_factory = registry.resolve(ComponentSlot.MEMORY_SYSTEM, spec.memory_system)
        mem_config = mem_factory.config_model.model_validate(spec.memory_system_config)
        context_manager: ContextManager | None = await mem_factory.create(
            mem_config, chain_with_provider
        )
    else:
        context_manager = inputs.context_manager
    prompt_config = dict(spec.system_prompt_config)
    if inputs.project_dir is not None and "path" in prompt_config:
        prompt_path = Path(prompt_config["path"])
        if not prompt_path.is_absolute():
            prompt_config["path"] = str(inputs.project_dir / prompt_path)
    prompt_provider: SystemPromptProvider = await _resolve_single(
        registry,
        ComponentSlot.SYSTEM_PROMPT_PROVIDER,
        spec.system_prompt_provider,
        prompt_config,
        chain,
    )
    memory_config = _merge_memory(inputs.memory_config, spec.memory_overrides)
    tool_manager = inputs.tool_manager or InMemoryToolManager()
    if inputs.root_provider is not None:
        from modex_agent.tools.workspace_scoped import wrap_standard_tools

        tools = wrap_standard_tools(tools, inputs.root_provider)
    bash_tool = next((tool for tool in tools if tool.name == "bash"), None)
    for tool in tools:
        tool_manager.register(
            inputs.tool_transform(tool) if inputs.tool_transform is not None else tool
        )
    # Structural bash+bash_input pairing: when the roster-resolved ``bash``
    # is a persistent shell, its stdin-answer companion shares the session —
    # a persistent shell without it deadlocks on interactive prompts
    # (commands have no default timeout). Single convergence point serving
    # both callers (Stage 4 mains and AgentTemplate subagents); idempotent,
    # and a no-op for CommandTool/SubprocessTool bash and bash-less rosters.
    from modex_agent.tools.terminal.persistent_bash import ensure_input_companion

    ensure_input_companion(
        tool_manager,
        bash_tool,
        tool_transform=inputs.tool_transform,
    )
    # Per-agent MCP loading (ticket 10): one FW path for mains and subs —
    # the selection rides the spec, the shared-connection handle comes from
    # the workspace layer of the chain (WorkspaceContext.mcp_registry,
    # ADR-0017). Fail-soft inside the loader; the live backend is returned
    # so the caller keeps the connection-lifecycle handle.
    mcp_backend: Any | None = None
    if spec.mcp_servers:
        from modex_agent.tools.mcp_loader import load_per_agent_mcp

        mcp_backend = await load_per_agent_mcp(
            tool_manager,
            list(spec.mcp_servers),
            inputs.project_dir or Path("."),
            spec.agent_name,
            registry=chain.mcp_registry,
            tool_transform=inputs.tool_transform,
        )
    system_prompt = await prompt_provider.get_or_refresh()
    hook_runner = HookRunner()
    await _dispatch_hooks(spec, registry, chain, hook_runner, inputs.memory_system)
    # Roster hooks and code-wired extra_hooks may name the same hook; the
    # name-based dedup keeps one instance (roster dispatch wins).
    seen_hook_names = {hook_spec.hook.name for hook_spec in hook_runner.hook_specs}
    for hook in inputs.extra_hooks:
        if hook.name in seen_hook_names:
            logger.debug(
                "extra hook %r duplicates a roster-dispatched hook; skipping",
                hook.name,
            )
            continue
        hook_runner.add(HookSpec(hook=hook))
        seen_hook_names.add(hook.name)

    # Capability-section anchor (SPEC §7.3): the merged prompt providers
    # (spec.capabilities iteration order; within one wiring, the
    # capability's own section order) feed the capability-section anchor
    # of the native memory context manager — the sections must be set
    # before the first load(), which happens at runtime after this
    # assembly returns.
    merged_sections: list[SystemPromptProvider] = [
        provider
        for compiled_cap in spec.capabilities
        for provider in capability_wirings[compiled_cap.name].prompt_providers
    ]
    if merged_sections:
        # isinstance is justified at this extension boundary: a custom
        # MEMORY_SYSTEM replaces the whole prompt assembly (SPEC Errata-7
        # replacement-face semantics — the same class of loss as
        # Errata-8(c)), so capability sections are native-only. The custom
        # owner opted out of native prompt assembly; skip, never raise.
        if isinstance(context_manager, MemorySystemContextManager):
            context_manager.set_capability_sections(tuple(merged_sections))
        else:
            logger.debug(
                "Capability sections for agent %r skipped: context manager "
                "%s is not the native MemorySystemContextManager (a custom "
                "memory system replaces prompt assembly)",
                spec.agent_name,
                type(context_manager).__name__,
            )

    comm_kind = (
        AgentCommKind.SUBAGENT if spec.agent_type is AgentType.native_sub else AgentCommKind.NORMAL
    )
    descriptor = AgentDescriptor(
        address=AgentAddress(name=spec.agent_name),
        llm_config=AgentLLMConfig(
            model=inputs.llm_defaults.model,
            temperature=inputs.llm_defaults.temperature,
            max_output_tokens=inputs.llm_defaults.max_output_tokens,
            reasoning_effort=inputs.llm_defaults.reasoning_effort,
            model_info=inputs.llm_defaults.model_info,
        ),
        system_prompt_template=system_prompt,
        max_iterations=spec.max_iterations,
        execution_strategy=inputs.execution_strategy,
        context_strategy=ContextStrategy.PERSISTENT,
        safety_policy=inputs.safety,
        comm_kind=comm_kind,
        memory_config=memory_config,
        roles=list(spec.roles),
        role_description=spec.description,
    )
    instance = await inputs.agent_factory.create_agent(
        descriptor,
        broker=inputs.broker,
        tool_manager=tool_manager,
        skill_resolver=inputs.skill_resolver,
        context_manager=context_manager,
        output_adapter=inputs.output_adapter,
        llm_provider=provider,
    )
    if instance.pipeline is not None and instance.pipeline.hook_runner is not None:
        instance.pipeline.hook_runner.extend(hook_runner.hook_specs)
    elif hook_runner.hook_specs:
        logger.warning(
            "Roster hooks %s dispatched for %r have no pipeline hook runner "
            "to land on — the agent factory produced a pipeline without one; "
            "the hooks are dropped",
            [spec.hook.name for spec in hook_runner.hook_specs],
            spec.agent_name,
        )
    # standalone single-agent assembly — no resident registry
    if inputs.pool is not None:
        await inputs.pool.register_resident(descriptor, instance)
    if (
        parent_session is not None
        and inputs.on_subagent_created is not None
        and inputs.pool is not None
    ):
        await inputs.on_subagent_created(
            f"{invocation_id or ''}.{spec.agent_name}",
            parent_session,
        )
    return NativeAssemblyResult(
        descriptor=descriptor,
        instance=instance,
        tool_manager=tool_manager,
        llm_provider=provider,
        system_prompt_provider=prompt_provider,
        system_prompt=system_prompt,
        memory_config=memory_config,
        hook_runner=hook_runner,
        mcp_backend=mcp_backend,
        capability_wirings=capability_wirings,
    )
