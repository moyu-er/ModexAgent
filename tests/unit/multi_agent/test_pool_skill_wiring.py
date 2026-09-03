"""Integration test: verify the bound skill resolver propagates from factory to pipeline.

Reproduces the pool-mode bug shape where /skill commands produce "Unknown
command" because the pipeline's skill resolver is None at runtime — now
pinned against the SkillResolver seam (plan §11).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from modex_agent.commands.handlers import SkillCommandHandler
from modex_agent.commands.models import CommandContext, SlashCommandInvocation
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.types import InputMessage
from modex_agent.messaging.broker import AddressKind
from modex_agent.messaging.broker_memory import InMemoryMessageBroker
from modex_agent.multi_agent import AgentPool, DefaultAgentFactory
from modex_agent.multi_agent.address import AgentAddress
from modex_agent.multi_agent.descriptor import AgentDescriptor
from modex_agent.plugins.defaults.capabilities.skills.catalog import SkillCatalog
from modex_agent.plugins.defaults.capabilities.skills.supply import build_skill_catalog


def _make_skill_catalog(tmp: Path) -> SkillCatalog:
    """Create a temp skill directory and return a catalog over it."""
    skills_root = tmp / "skills" / "main" / "main"
    skill_dir = skills_root / "test-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: test-skill\ndescription: A test skill\n---\n\n# Test Skill\nHello.",
        encoding="utf-8",
    )
    return build_skill_catalog([skills_root])


@pytest.mark.asyncio
async def test_factory_threads_explicit_skill_resolver_to_pipeline(tmp_path: Path) -> None:
    catalog = _make_skill_catalog(tmp_path)
    broker = InMemoryMessageBroker()
    await broker.start()
    try:
        factory = DefaultAgentFactory(default_llm_provider=MagicMock())
        descriptor = AgentDescriptor(
            address=AgentAddress(kind=AddressKind.AGENT, name="main"),
        )
        instance = await factory.create_agent(
            descriptor,
            broker=broker,
            skill_resolver=catalog,
        )

        pipeline = instance.pipeline
        assert pipeline is not None
        resolver = pipeline.skill_resolver
        assert resolver is not None
        assert resolver is catalog

        resolved = await resolver.resolve_command("test-skill", "")
        assert resolved is not None

        handler = SkillCommandHandler()
        invocation = SlashCommandInvocation(command="test-skill", args="", raw="/test-skill")
        context = CommandContext(
            session_id="s1",
            input_msg=InputMessage(content="/test-skill", session=SessionInfo.from_str("s1")),
            agent_name="main",
            skill_resolver=resolver,
        )
        result = await handler.handle(invocation, context)
        assert result.user_content is not None
        assert "test-skill" in result.user_content
    finally:
        await broker.stop()


@pytest.mark.asyncio
async def test_public_materialization_keeps_root_skills_out_of_vetoed_subagent(
    tmp_path: Path,
) -> None:
    from modex_agent.core.llm_struct import RuntimeSafetyPolicy
    from modex_agent.core.provider import LLMProvider
    from modex_agent.core.session_id import SessionIdFactory
    from modex_agent.multi_agent.execution_strategy import PoolAssemblyContext
    from modex_agent.multi_agent.materialize_deps import AgentMaterializeDeps
    from modex_agent.multi_agent.session_tree.manager import SessionTreeManager
    from modex_agent.multi_agent.template import AgentTemplate
    from modex_agent.plugins.defaults import DefaultPlugin
    from modex_agent.plugins.defaults.capabilities.skills.supply import build_skills_supply
    from modex_agent.plugins.defaults.capabilities.subagents import SubagentsSupply
    from modex_agent.plugins.loader import PluginRegistrationContext
    from modex_agent.plugins.registry import ComponentRegistry
    from modex_agent.scope.compiler import compile_scope
    from modex_agent.scope.spec import AgentSpec, PoolSpec, ScopeKind, ScopeSpec
    from modex_agent.tools.presets import ToolPreset
    from modex_agent.workspace.context import WorkspaceContext
    from modex_agent.workspace.paths import WorkspacePaths
    from modex_agent.workspace.scope_path import ScopePath

    skills_root = tmp_path / "skills" / "main" / "main"
    skill_dir = skills_root / "root-only"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: root-only\ndescription: Root only\n---\n\n# Root Only",
        encoding="utf-8",
    )
    skills_supply = build_skills_supply(
        pool_name="main",
        skill_root_for_agent={"main": [skills_root]},
    )
    root_resolver = skills_supply.resolver_for("main")

    with pytest.raises(TypeError):
        DefaultAgentFactory(skill_resolver=root_resolver)  # type: ignore[call-arg]

    registry = ComponentRegistry()
    with PluginRegistrationContext(registry) as registration:
        DefaultPlugin().register(registration)
    root = AgentSpec(name="main", toolset=ToolPreset.NONE)
    sub = AgentSpec(
        name="scout",
        parent="main",
        toolset=ToolPreset.NONE,
        capabilities={"skills": False},
    )
    pool_spec = PoolSpec(name="main", agents=[root, sub])
    compilation = compile_scope(
        ScopeSpec(kind=ScopeKind.POOL, pool=pool_spec),
        workspace_ctx=WorkspaceContext(
            target=tmp_path,
            paths=WorkspacePaths(root=tmp_path / "data"),
            is_home=False,
        ),
        registry=registry,
    )
    compiled_sub = next(agent for agent in compilation.agents if agent.spec.agent_name == "scout")

    provider = MagicMock(spec=LLMProvider)
    factory = DefaultAgentFactory(default_llm_provider=provider)
    broker = InMemoryMessageBroker()
    await broker.start()
    pool = AgentPool(broker=broker, agent_factory=factory)
    pool_assembly = MagicMock(spec=PoolAssemblyContext)
    pool_assembly.pool_name = "main"
    pool_assembly.pool_spec = pool_spec
    pool_assembly.pool_data = None
    pool_assembly.project_dir = tmp_path
    pool_assembly.peer_links = ()
    pool_assembly.control_origin = "http://127.0.0.1:21800"
    deps = AgentMaterializeDeps(
        agent_factory=factory,
        pool=pool,
        session_factory=SessionIdFactory(),
        broker=broker,
        tree=MagicMock(spec=SessionTreeManager),
        safety=RuntimeSafetyPolicy(),
        llm_model="test-model",
        llm_provider=provider,
        project_dir=tmp_path,
        data_dir=tmp_path / "data",
        component_registry=registry,
        pool_assembly_ctx=pool_assembly,
        scope_path=ScopePath(workspace_root=tmp_path, pool_name="main"),
        capability_supply={
            "skills": skills_supply,
            "subagents": SubagentsSupply(service=MagicMock()),
        },
    )
    template = AgentTemplate(
        spec=sub,
        toolset_profile=compiled_sub.defaults.toolset_profile,
        compiled_spec=compiled_sub.spec,
    )

    try:
        instance = await template.materialize(
            parent_session=SessionIdFactory().create(agent_name="main"),
            invocation_id="inv1",
            deps=deps,
        )
        assert instance.pipeline is not None
        assert instance.pipeline.skill_resolver is None
        assert await root_resolver.resolve_command("root-only", "") is not None
    finally:
        await pool.shutdown_all()
        await broker.stop()


@pytest.mark.asyncio
async def test_pooled_assembly_uses_one_disk_catalog_per_native_agent(
    tmp_path: Path,
) -> None:
    from collections.abc import AsyncIterator

    from pydantic import BaseModel, ConfigDict

    from modex_agent.core.context import ContextManager
    from modex_agent.core.llm_request import LLMRequest
    from modex_agent.core.llm_struct import RuntimeSafetyPolicy
    from modex_agent.core.provider import LLMProvider
    from modex_agent.core.scope import MemoryAgentRole
    from modex_agent.core.session_id import SessionIdFactory
    from modex_agent.core.stream_events import LLMStreamEvent
    from modex_agent.ioc.factories.descriptors import build_session_only_memory
    from modex_agent.multi_agent import SessionRetentionPolicy
    from modex_agent.multi_agent.bus import LocalAgentMessageBus
    from modex_agent.multi_agent.execution_strategy import (
        ExecutionStrategy,
        PoolAssemblyContext,
        StrategyAssembly,
    )
    from modex_agent.multi_agent.inbox.consumer import InboxConsumer
    from modex_agent.multi_agent.inbox.producer import InboxProducer
    from modex_agent.multi_agent.inbox.server_memory import InMemoryInboxServer
    from modex_agent.multi_agent.materialize_deps import AgentMaterializeDeps
    from modex_agent.multi_agent.session_tree.manager import SessionTreeManager
    from modex_agent.multi_agent.template import AgentTemplate
    from modex_agent.multi_agent.template_registry import AgentTemplateRegistry
    from modex_agent.pipeline.turn_session_registry import TurnSessionRegistry
    from modex_agent.plugins.abc import SimpleFactory
    from modex_agent.plugins.assembly.builder import AssemblyBuilder
    from modex_agent.plugins.assembly.context import AssemblyContext, SupplyInfra
    from modex_agent.plugins.assembly.native_core import LlmDefaults, NativeAssemblyInputs
    from modex_agent.plugins.assembly.pipeline import AssemblyPipeline
    from modex_agent.plugins.assembly.spec import AssemblySpec
    from modex_agent.plugins.assembly.stages.agent_assemble import AgentAssembleStage
    from modex_agent.plugins.assembly.stages.infra_assemble import InfraAssembleStage
    from modex_agent.plugins.assembly.stages.pool_assemble import PoolAssembleStage
    from modex_agent.plugins.assembly.stages.workspace_materialize import (
        WorkspaceMaterializeStage,
    )
    from modex_agent.plugins.defaults import DefaultPlugin
    from modex_agent.plugins.defaults.capabilities.skills import require_skills_supply
    from modex_agent.plugins.loader import PluginRegistrationContext
    from modex_agent.plugins.registry import ComponentRegistry
    from modex_agent.scope.compiler import compile_scope
    from modex_agent.scope.spec import AgentSpec, PoolSpec, ScopeKind, ScopeSpec
    from modex_agent.tools.presets import ToolPreset
    from modex_agent.workspace.context import WorkspaceContext
    from modex_agent.workspace.paths import WorkspacePaths
    from modex_agent.workspace.scope_path import ScopePath

    class _EmptyConfig(BaseModel):
        model_config = ConfigDict(frozen=True, extra="forbid")

    class _NoCallProvider(LLMProvider):
        def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
            del request
            raise AssertionError("pooled Skills assembly must not call the model")

        def get_default_model(self) -> str:
            return "test-model"

    class _AssemblyStrategy(ExecutionStrategy):
        def __init__(self, context_manager: ContextManager) -> None:
            self._context_manager = context_manager

        @property
        def name(self) -> str:
            return "react"

        async def assemble_main(self, ctx: PoolAssemblyContext) -> StrategyAssembly:
            del ctx
            return StrategyAssembly(context_manager=self._context_manager)

        def validate_pool_spec(self, pool: PoolSpec) -> None:
            del pool

    pool_name = "writers"
    main_name = "lead"
    sub_name = "scout"
    for agent_name, skill_name in (
        (main_name, "main-only"),
        (sub_name, "sub-only"),
    ):
        skill_dir = tmp_path / "skills" / pool_name / agent_name / skill_name
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: {skill_name}\ndescription: {agent_name} disk skill\n---\n\n"
            f"# {skill_name}\n",
            encoding="utf-8",
        )

    workspace_ctx = WorkspaceContext(
        target=tmp_path,
        paths=WorkspacePaths(root=tmp_path / "data"),
        is_home=False,
    )
    root = AgentSpec(name=main_name, toolset=ToolPreset.NONE)
    sub = AgentSpec(name=sub_name, parent=main_name, toolset=ToolPreset.NONE)
    pool_spec = PoolSpec(name=pool_name, agents=[root, sub])
    main_context = build_session_only_memory(
        cfg=None,
        workspace=tmp_path / "data" / "memory" / pool_name,
        agent_id=main_name,
        agent_role=MemoryAgentRole.MAIN,
    )
    provider = _NoCallProvider()
    registry = ComponentRegistry()
    with PluginRegistrationContext(registry) as registration:
        DefaultPlugin().register(registration)
        registration.register_execution_strategy(
            "react",
            SimpleFactory(_AssemblyStrategy(main_context), _EmptyConfig),
        )

    compilation = compile_scope(
        ScopeSpec(kind=ScopeKind.POOL, pool=pool_spec),
        workspace_ctx=workspace_ctx,
        registry=registry,
    )
    compiled_main = next(
        agent for agent in compilation.agents if agent.spec.agent_name == main_name
    )
    compiled_sub = next(
        agent for agent in compilation.agents if agent.spec.agent_name == sub_name
    )
    template = AgentTemplate(
        spec=sub,
        toolset_profile=compiled_sub.defaults.toolset_profile,
        compiled_spec=compiled_sub.spec,
    )
    template_registry = AgentTemplateRegistry(
        seeded={pool_name: {sub_name: template}},
    )

    broker = InMemoryMessageBroker()
    await broker.start()
    inbox = InMemoryInboxServer()
    bus = LocalAgentMessageBus(
        producer=InboxProducer(server=inbox),
        consumer=InboxConsumer(server=inbox),
    )
    factory = DefaultAgentFactory(default_llm_provider=provider)
    pool = AgentPool(broker=broker, agent_factory=factory, agent_bus=bus)
    pool.template_registry = template_registry
    tree = MagicMock(spec=SessionTreeManager)
    pool.tree = tree
    scope_path = ScopePath(workspace_root=tmp_path, pool_name=pool_name)
    pool_assembly_ctx = PoolAssemblyContext(
        pool_name=pool_name,
        pool_spec=pool_spec,
        project_dir=tmp_path,
        data_dir=tmp_path / "data",
        broker=broker,
        inbox_server=inbox,
        agent_bus=bus,
        output_adapter=MagicMock(),
        safety=RuntimeSafetyPolicy(),
        retention=SessionRetentionPolicy(),
        registry=TurnSessionRegistry(),
        scope_path=scope_path,
    )
    infra = SupplyInfra(
        pool_assembly_ctx=pool_assembly_ctx,
        pool=pool,
        pool_specs=(compiled_main.spec, compiled_sub.spec),
        default_llm_provider=provider,
    )

    def build_native_inputs(
        spec: AssemblySpec,
        builder: AssemblyBuilder,
        ctx: AssemblyContext,
    ) -> NativeAssemblyInputs:
        del builder
        assert ctx.pool_runtime is not None
        supply = require_skills_supply(ctx.pool_runtime.capability_supply)
        return NativeAssemblyInputs(
            agent_factory=factory,
            broker=broker,
            llm_defaults=LlmDefaults(model="test-model"),
            pool=pool,
            context_manager=main_context,
            llm_provider=provider,
            skill_resolver=supply.resolver_for(spec.agent_name),
            project_dir=tmp_path,
        )

    pipeline = AssemblyPipeline(
        workspace_materialize=WorkspaceMaterializeStage(),
        infra_assemble=InfraAssembleStage(),
        pool_assemble=PoolAssembleStage(),
        agent_assemble=AgentAssembleStage(build_native_inputs),
    )

    try:
        assembled = await pipeline.run(
            compiled_main.spec,
            AssemblyContext(
                registry=registry,
                workspace_ctx=workspace_ctx,
                workspace_resources=object(),
                infra=infra,
            ),
        )
        assert assembled.propagated_context is not None
        assert assembled.propagated_context.pool_runtime is not None
        supply = require_skills_supply(
            assembled.propagated_context.pool_runtime.capability_supply
        )
        main_catalog = supply.catalog_for(main_name)
        sub_catalog = supply.catalog_for(sub_name)

        main_instance = assembled.agent
        assert main_instance is not None
        assert main_instance.pipeline is not None
        assert main_instance.pipeline.skill_resolver is main_catalog
        assert await main_catalog.resolve_command("main-only", "") is not None
        assert await main_catalog.resolve_command("sub-only", "") is None

        deps = AgentMaterializeDeps(
            agent_factory=factory,
            pool=pool,
            session_factory=SessionIdFactory(),
            broker=broker,
            tree=tree,
            safety=RuntimeSafetyPolicy(),
            llm_model="test-model",
            llm_provider=provider,
            project_dir=tmp_path,
            data_dir=tmp_path / "data",
            component_registry=registry,
            pool_assembly_ctx=pool_assembly_ctx,
            scope_path=scope_path,
            capability_supply=assembled.propagated_context.pool_runtime.capability_supply,
        )
        sub_instance = await template.materialize(
            parent_session=SessionIdFactory().create(agent_name=main_name),
            invocation_id="inv1",
            deps=deps,
        )
        assert sub_instance.pipeline is not None
        assert sub_instance.pipeline.skill_resolver is sub_catalog
        assert main_catalog is not sub_catalog
        assert await sub_catalog.resolve_command("sub-only", "") is not None
        assert await sub_catalog.resolve_command("main-only", "") is None

        main_state = await main_instance.context_manager.load("main-session")
        sub_state = await sub_instance.context_manager.load("sub-session")
        assert main_state.system_prompt_pipeline is not None
        assert sub_state.system_prompt_pipeline is not None
        main_prompt = await main_state.system_prompt_pipeline.get_or_refresh()
        sub_prompt = await sub_state.system_prompt_pipeline.get_or_refresh()
        assert 'name="main-only"' in main_prompt
        assert 'name="sub-only"' not in main_prompt
        assert 'name="sub-only"' in sub_prompt
        assert 'name="main-only"' not in sub_prompt
    finally:
        await pool.shutdown_all()
        await broker.stop()
