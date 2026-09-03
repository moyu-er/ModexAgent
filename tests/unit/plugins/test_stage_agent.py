from __future__ import annotations

import dataclasses
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from modex_agent.core.prompt import SystemPromptProvider
from modex_agent.core.tool_manager import Tool
from modex_agent.hook.runner import HookRunner
from modex_agent.memory.context import ContextManager, InMemoryContextManager
from modex_agent.multi_agent.descriptor import AgentInstance
from modex_agent.multi_agent.factory import AgentFactory
from modex_agent.plugins.abc import (
    AgentType,
    ComponentFactory,
    ComponentSlot,
    HookRunnerKind,
    SimpleFactory,
)
from modex_agent.plugins.assembly.builder import AssemblyBuilder
from modex_agent.plugins.assembly.context import AssemblyContext
from modex_agent.plugins.assembly.native_core import (
    LlmDefaults,
    NativeAssemblyInputs,
    NativeAssemblyResult,
    assemble_native_agent,
)
from modex_agent.plugins.assembly.spec import AssemblySpec, MemoryOverrides
from modex_agent.plugins.assembly.stages.agent_assemble import AgentAssembleStage
from modex_agent.plugins.registry import ComponentNotFoundError, ComponentRegistry
from modex_agent.workspace.context import WorkspaceContext
from modex_agent.workspace.paths import WorkspacePaths


class _EmptyConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class _StrictConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    required_field: str


class _MemorySystemFactory(ComponentFactory):
    config_model = _EmptyConfig

    def __init__(self, context_manager: ContextManager) -> None:
        self.context_manager = context_manager
        self.received_context: AssemblyContext | None = None

    async def create(
        self,
        _config: BaseModel,
        ctx: AssemblyContext,
    ) -> ContextManager:
        self.received_context = ctx
        return self.context_manager


class _PromptProvider(SystemPromptProvider):
    async def _fetch_version(self) -> str:
        return "v1"

    async def _fetch_content(self) -> str:
        return "assembled prompt"


def _workspace() -> WorkspaceContext:
    root = Path("/tmp/test-native-core")
    return WorkspaceContext(
        target=root,
        paths=WorkspacePaths(root=root),
        is_home=False,
    )


def _spec(
    *,
    agent_type: AgentType = AgentType.native_main,
    hooks: list[str] | None = None,
    tool_configs: dict[str, dict[str, str]] | None = None,
) -> AssemblySpec:
    return AssemblySpec(
        agent_type=agent_type,
        agent_name="worker",
        pool_name="pool",
        description="test worker",
        max_iterations=23,
        roles=["tester"],
        tools=["tool"],
        tool_configs=tool_configs or {},
        hooks=hooks or [],
        llm_provider="llm",
        system_prompt_provider="prompt",
        system_prompt_config={},
        memory_overrides=MemoryOverrides(),
        execution_strategy="react",
        workspace_ctx=_workspace(),
    )


def _registry(
    *,
    hooks: dict[str, SimpleFactory] | None = None,
    tool_config_model: type[BaseModel] = _EmptyConfig,
) -> ComponentRegistry:
    registry = ComponentRegistry()
    tool = MagicMock(spec=Tool)
    tool.name = "tool"
    registry.register(
        ComponentSlot.TOOL,
        "tool",
        SimpleFactory(tool, tool_config_model),
    )
    registry.register(
        ComponentSlot.LLM_PROVIDER,
        "llm",
        SimpleFactory(MagicMock(), _EmptyConfig),
    )
    registry.register(
        ComponentSlot.SYSTEM_PROMPT_PROVIDER,
        "prompt",
        SimpleFactory(_PromptProvider(), _EmptyConfig),
    )
    for name, factory in (hooks or {}).items():
        registry.register(ComponentSlot.HOOK, name, factory)
    return registry


def _harness(
    registry: ComponentRegistry,
    *,
    memory_system: MagicMock | None = None,
) -> tuple[AssemblyContext, NativeAssemblyInputs, AgentInstance, MagicMock]:
    context_manager = InMemoryContextManager(base_system_prompt="")
    instance_hook_runner = HookRunner()
    pipeline = MagicMock()
    pipeline.hook_runner = instance_hook_runner
    instance = AgentInstance(
        descriptor=MagicMock(),
        context_manager=context_manager,
        pipeline=pipeline,
    )
    agent_factory = MagicMock(spec=AgentFactory)
    agent_factory.create_agent = AsyncMock(return_value=instance)
    pool = MagicMock()
    pool.register_resident = AsyncMock(return_value=instance)
    inputs = NativeAssemblyInputs(
        agent_factory=agent_factory,
        broker=MagicMock(),
        llm_defaults=LlmDefaults(model="test/model"),
        pool=pool,
        context_manager=context_manager,
        memory_system=memory_system or MagicMock(),
        skill_resolver=MagicMock(),
        project_dir=_workspace().target,
    )
    ctx = AssemblyContext(
        registry=registry,
        workspace_ctx=_workspace(),
    )
    return ctx, inputs, instance, pool


async def test_native_core_resolves_seven_slots_and_registers_real_instance() -> None:
    registry = _registry()
    ctx, inputs, instance, pool = _harness(registry)

    result = await assemble_native_agent(_spec(), registry, inputs, ctx=ctx)

    assert isinstance(result, NativeAssemblyResult)
    assert result.instance is instance
    assert result.descriptor.max_iterations == 23
    assert result.descriptor.roles == ["tester"]
    assert result.tool_manager.list_tools() == ["tool"]
    assert isinstance(result.system_prompt, str)
    pool.register_resident.assert_awaited_once_with(result.descriptor, instance)


async def test_native_core_uses_configured_memory_system_context_manager() -> None:
    registry = _registry()
    ctx, inputs, _, _ = _harness(registry)
    fallback_context_manager = inputs.context_manager
    probe_context_manager = InMemoryContextManager(base_system_prompt="probe")
    memory_factory = _MemorySystemFactory(probe_context_manager)
    registry.register(ComponentSlot.MEMORY_SYSTEM, "probe", memory_factory)
    agent_factory = MagicMock(spec=AgentFactory)
    agent_factory.create_agent = AsyncMock(
        side_effect=lambda descriptor, **kwargs: AgentInstance(
            descriptor=descriptor,
            context_manager=kwargs["context_manager"],
        )
    )
    inputs = NativeAssemblyInputs(
        agent_factory=agent_factory,
        broker=inputs.broker,
        llm_defaults=inputs.llm_defaults,
        pool=inputs.pool,
        context_manager=fallback_context_manager,
        memory_system=inputs.memory_system,
        skill_resolver=inputs.skill_resolver,
        project_dir=inputs.project_dir,
    )
    spec = _spec().model_copy(update={"memory_system": "probe"})

    result = await assemble_native_agent(spec, registry, inputs, ctx=ctx)

    assert result.instance.context_manager is probe_context_manager
    assert result.instance.context_manager is not fallback_context_manager
    assert memory_factory.received_context is not None
    assert memory_factory.received_context.llm_provider is result.llm_provider


def test_native_assembly_types_follow_value_and_runtime_object_rules() -> None:
    assert issubclass(LlmDefaults, BaseModel)
    assert not dataclasses.is_dataclass(NativeAssemblyInputs)
    assert not dataclasses.is_dataclass(NativeAssemblyResult)


async def test_native_core_resolves_system_prompt_provider_from_spec() -> None:
    registry = _registry()
    ctx, inputs, _, _ = _harness(registry)
    prompt_factory = registry.resolve(ComponentSlot.SYSTEM_PROMPT_PROVIDER, "prompt")
    configured_prompt_provider = _PromptProvider()
    create_prompt = AsyncMock(return_value=configured_prompt_provider)

    with patch.object(prompt_factory, "create", create_prompt):
        result = await assemble_native_agent(_spec(), registry, inputs, ctx=ctx)

    assert result.system_prompt_provider is configured_prompt_provider
    create_prompt.assert_awaited_once()


def test_llm_defaults_is_exported_from_plugins_public_api() -> None:
    from modex_agent.plugins import LlmDefaults as PublicLlmDefaults

    assert PublicLlmDefaults is LlmDefaults


async def test_stage_four_sets_authoritative_instance_and_descriptor() -> None:
    registry = _registry()
    ctx, inputs, instance, _ = _harness(registry)
    builder = AssemblyBuilder()
    stage = AgentAssembleStage(lambda spec, builder, ctx: inputs)

    await stage.process(_spec(), builder, ctx)

    assert builder.agent is instance
    assert builder.descriptor.address.name == "worker"


async def test_react_hook_is_filtered_then_wired_to_runtime_runner() -> None:
    main_hook = MagicMock()
    sub_hook = MagicMock()
    registry = _registry(
        hooks={
            "main": SimpleFactory(
                main_hook,
                _EmptyConfig,
                applies_to={AgentType.native_main},
                hook_runner=HookRunnerKind.react,
            ),
            "sub": SimpleFactory(
                sub_hook,
                _EmptyConfig,
                applies_to={AgentType.native_sub},
                hook_runner=HookRunnerKind.react,
            ),
        }
    )
    ctx, inputs, instance, _ = _harness(registry)

    result = await assemble_native_agent(
        _spec(hooks=["main", "sub"]),
        registry,
        inputs,
        ctx=ctx,
    )

    assert [item.hook for item in result.hook_runner.hook_specs] == [main_hook]
    assert [item.hook for item in instance.pipeline.hook_runner.hook_specs] == [main_hook]


async def test_memory_hook_is_dispatched_to_memory_system() -> None:
    memory_hook = MagicMock()
    registry = _registry(
        hooks={
            "memory": SimpleFactory(
                memory_hook,
                _EmptyConfig,
                hook_runner=HookRunnerKind.memory,
            )
        }
    )
    memory_system = MagicMock()
    ctx, inputs, _, _ = _harness(registry, memory_system=memory_system)

    await assemble_native_agent(
        _spec(hooks=["memory"]),
        registry,
        inputs,
        ctx=ctx,
    )

    memory_system.add_cleanup_hook.assert_called_once_with(memory_hook)


async def test_memory_hook_with_replaced_memory_system_raises() -> None:
    """Orphan-hook guard: a memory-runner hook combined with a roster
    memory-system override must fail loudly instead of silently attaching
    to the orphaned framework memory system (plan issue I2)."""
    memory_hook = MagicMock()
    registry = _registry(
        hooks={
            "probe_memory_hook": SimpleFactory(
                memory_hook,
                _EmptyConfig,
                hook_runner=HookRunnerKind.memory,
            )
        }
    )
    registry.register(
        ComponentSlot.MEMORY_SYSTEM,
        "probe",
        _MemorySystemFactory(InMemoryContextManager(base_system_prompt="probe")),
    )
    ctx, inputs, _, _ = _harness(registry)
    spec = _spec(hooks=["probe_memory_hook"]).model_copy(
        update={"memory_system": "probe"}
    )

    with pytest.raises(ValueError, match="probe_memory_hook"):
        await assemble_native_agent(spec, registry, inputs, ctx=ctx)


async def test_react_hook_with_replaced_memory_system_still_dispatches() -> None:
    """React-runner hooks are unaffected by the orphan-hook guard — only
    memory-runner hooks conflict with a replaced memory system."""
    react_hook = MagicMock()
    registry = _registry(
        hooks={
            "react": SimpleFactory(
                react_hook,
                _EmptyConfig,
                hook_runner=HookRunnerKind.react,
            )
        }
    )
    registry.register(
        ComponentSlot.MEMORY_SYSTEM,
        "probe",
        _MemorySystemFactory(InMemoryContextManager(base_system_prompt="probe")),
    )
    ctx, inputs, _, _ = _harness(registry)
    spec = _spec(hooks=["react"]).model_copy(update={"memory_system": "probe"})

    result = await assemble_native_agent(spec, registry, inputs, ctx=ctx)

    assert [item.hook for item in result.hook_runner.hook_specs] == [react_hook]


async def test_invalid_component_config_fails_before_factory_create() -> None:
    registry = _registry(tool_config_model=_StrictConfig)
    ctx, inputs, _, _ = _harness(registry)

    with pytest.raises(ValidationError):
        await assemble_native_agent(_spec(), registry, inputs, ctx=ctx)


async def test_unknown_tool_name_raises_component_not_found() -> None:
    """An unknown tool name in the roster reaches assembly as a LOUD
    ComponentNotFoundError — never a silent skip."""
    registry = _registry()
    ctx, inputs, _, _ = _harness(registry)
    spec = _spec().model_copy(update={"tools": ["tool", "no_such_tool"]})

    with pytest.raises(ComponentNotFoundError, match="no_such_tool"):
        await assemble_native_agent(spec, registry, inputs, ctx=ctx)


async def test_unknown_hook_name_raises_component_not_found() -> None:
    """An unknown hook name in the roster reaches assembly as a LOUD
    ComponentNotFoundError — never a silent skip."""
    registry = _registry()
    ctx, inputs, _, _ = _harness(registry)

    with pytest.raises(ComponentNotFoundError, match="no_such_hook"):
        await assemble_native_agent(
            _spec(hooks=["no_such_hook"]), registry, inputs, ctx=ctx
        )


async def test_memory_hook_without_any_memory_system_raises() -> None:
    """A memory-runner hook with NO memory system at all is a configuration
    error — loud ValueError, not a silent drop."""
    memory_hook = MagicMock()
    registry = _registry(
        hooks={
            "memory": SimpleFactory(
                memory_hook,
                _EmptyConfig,
                hook_runner=HookRunnerKind.memory,
            )
        }
    )
    ctx, inputs, _, _ = _harness(registry)
    inputs = NativeAssemblyInputs(
        agent_factory=inputs.agent_factory,
        broker=inputs.broker,
        llm_defaults=inputs.llm_defaults,
        pool=inputs.pool,
        context_manager=inputs.context_manager,
        memory_system=None,
        skill_resolver=inputs.skill_resolver,
        project_dir=inputs.project_dir,
    )

    with pytest.raises(ValueError, match="Memory hook 'memory' requires a memory system"):
        await assemble_native_agent(
            _spec(hooks=["memory"]), registry, inputs, ctx=ctx
        )


async def test_extra_hook_duplicate_of_roster_hook_is_deduped() -> None:
    """M14: a hook name registered both via the roster and code-wired
    ``extra_hooks`` appears once — the roster-dispatched instance wins."""
    from modex_agent.hook.abc import Hook

    class _ProbeHook(Hook):
        pass

    class _OtherHook(Hook):
        pass

    roster_hook = _ProbeHook()
    registry = _registry(
        hooks={
            "probe": SimpleFactory(
                roster_hook,
                _EmptyConfig,
                hook_runner=HookRunnerKind.react,
            )
        }
    )
    ctx, inputs, _, _ = _harness(registry)
    duplicate_probe = _ProbeHook()
    other_hook = _OtherHook()
    inputs.extra_hooks = (duplicate_probe, other_hook)  # type: ignore[assignment]

    result = await assemble_native_agent(
        _spec(hooks=["probe"]), registry, inputs, ctx=ctx
    )

    hooks = [item.hook for item in result.hook_runner.hook_specs]
    assert hooks == [roster_hook, other_hook]
