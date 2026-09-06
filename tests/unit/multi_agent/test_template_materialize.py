"""Tests for AgentTemplate.materialize — the single construction path."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modex_agent.core import AgentCommKind
from modex_agent.core.agent import AgentContext
from modex_agent.core.llm_request import ReasoningEffort
from modex_agent.core.llm_struct import FinishReason, LLMResponse, RuntimeSafetyPolicy
from modex_agent.core.message import ChatMessage
from modex_agent.core.provider import CallbackStreamProvider
from modex_agent.core.session_id import SessionIdFactory, SessionInfo
from modex_agent.memory.cleanup_hooks import TodoReorientationHook
from modex_agent.multi_agent.context_fork import ContextForkBuilder
from modex_agent.multi_agent.descriptor import AgentInstance
from modex_agent.multi_agent.materialize_deps import AgentMaterializeDeps
from modex_agent.multi_agent.session_tree.manager import SessionTreeManager
from modex_agent.multi_agent.template import AgentTemplate
from modex_agent.plugins.registry import ComponentRegistry
from modex_agent.runtime.services import AgentRuntimeServices
from modex_agent.scope.compiler import compile_scope
from modex_agent.scope.spec import AgentSpec, PoolSpec, ScopeKind, ScopeSpec
from modex_agent.tools.workspace_scoped import WorkspaceRootProvider
from modex_agent.workspace.context import WorkspaceContext
from modex_agent.workspace.paths import WorkspacePaths
from modex_agent.workspace.scope_path import ScopePath


@pytest.fixture(autouse=True)
def _modexctl_location(monkeypatch, tmp_path):
    # These assembly tests do not invoke modexctl; its installation is unrelated.
    monkeypatch.setattr("modex_agent.plugins.defaults.hooks.resolve_modexctl_bin_dir", lambda: tmp_path)


class _StaticRootProvider(WorkspaceRootProvider):
    """Static workspace root — matches the deps' scope_path.workspace_root."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def current(self) -> Path:
        return self._root


async def _make_deps() -> tuple[AgentMaterializeDeps, MagicMock]:
    """Build deps with a mocked agent_factory + pool."""
    from modex_agent.multi_agent.execution_strategy import PoolAssemblyContext

    fake_instance = MagicMock()
    fake_instance.pipeline = MagicMock()
    from modex_agent.runtime.services import AgentRuntimeServices

    fake_instance.pipeline._turn_runner.turn_context_builder.runtime_services = (
        AgentRuntimeServices()
    )
    fake_instance.stop = AsyncMock()
    pool = MagicMock()
    pool.register_resident = AsyncMock()
    pool.get = MagicMock(return_value=None)  # no parent instance
    factory = MagicMock()
    factory.create_agent = AsyncMock(return_value=fake_instance)
    from modex_agent.plugins.defaults import DefaultPlugin
    from modex_agent.plugins.loader import ComponentRegistryLoader, PluginDiscoveryConfig
    from modex_agent.plugins.registry import ComponentRegistry

    registry = ComponentRegistry()
    await ComponentRegistryLoader.load(
        registry,
        PluginDiscoveryConfig(
            bundled_factories=(DefaultPlugin(),),
            project_plugin_paths=(),
        ),
    )
    # The declared pool tree the subagent_auto_send factory derives the
    # parent name from (the roster-dispatched hook's chain read) — every
    # template materialized through these deps is the "scout" sub. The
    # env-spec fields (project_dir / peer_links / control_origin) feed the
    # position-default native_env factory's chain derivation.
    pool_assembly = MagicMock(spec=PoolAssemblyContext)
    pool_assembly.pool_name = "main"
    pool_assembly.pool_spec = PoolSpec(
        name="main",
        agents=[AgentSpec(name="main"), AgentSpec(name="scout", parent="main")],
    )
    pool_assembly.pool_data = None
    pool_assembly.project_dir = Path("/ws")
    pool_assembly.peer_links = ()
    pool_assembly.control_origin = "http://127.0.0.1:21800"
    # The pool's subagents supply — the materialized sub's compiled spec
    # carries the subagents capability (non-root ⇒ derived send_to_agent
    # + the auto-send hook + the consultation section), whose assemble
    # and TOOL factories read the supply off the threaded mapping.
    # The skills supply joins it: skills auto-applies to every native
    # agent (plan §11.3), so the sub's assemble reads it here too.
    from modex_agent.plugins.defaults.capabilities.skills.supply import build_skills_supply
    from modex_agent.plugins.defaults.capabilities.subagents import SubagentsSupply

    deps = AgentMaterializeDeps(
        agent_factory=factory,
        pool=pool,
        session_factory=SessionIdFactory(),
        broker=MagicMock(),
        tree=MagicMock(spec=SessionTreeManager),
        safety=RuntimeSafetyPolicy(),
        llm_model="gpt-4o",
        llm_provider=MagicMock(),
        project_dir=None,  # skip prompt file read + MCP + skills
        root_provider=_StaticRootProvider(Path("/ws")),
        component_registry=registry,
        pool_assembly_ctx=pool_assembly,
        capability_supply={
            "subagents": SubagentsSupply(service=MagicMock()),
            "skills": build_skills_supply(
                pool_name="main", skill_root_for_agent={"scout": []}
            ),
        },
    )
    deps.context_fork_builder = ContextForkBuilder()
    deps.scope_path = ScopePath(workspace_root=Path("/ws"), pool_name="main")
    return deps, factory


def _workspace_ctx() -> WorkspaceContext:
    target = Path("/tmp/test_template_materialize")
    return WorkspaceContext(target=target, paths=WorkspacePaths(root=target), is_home=False)


def _compiled_template(name: str, **agent_kwargs: object) -> AgentTemplate:
    """Compile a two-agent tree (main root + named sub) and seed the sub's
    template exactly as the declaration road does (declared spec +
    position-derived profile + compiled assembly spec)."""
    from modex_agent.plugins.defaults import DefaultPlugin
    from modex_agent.plugins.loader import PluginRegistrationContext
    from modex_agent.plugins.registry import ComponentRegistry

    registry = ComponentRegistry()
    with PluginRegistrationContext(registry) as registration:
        DefaultPlugin().register(registration)

    declared = AgentSpec(name=name, parent="main", **agent_kwargs)  # type: ignore[arg-type]
    spec = ScopeSpec(
        kind=ScopeKind.POOL,
        pool=PoolSpec(name="main", agents=[AgentSpec(name="main"), declared]),
    )
    compilation = compile_scope(spec, workspace_ctx=_workspace_ctx(), registry=registry)
    compiled = next(a for a in compilation.agents if a.provenance.agent == name)
    return AgentTemplate(
        spec=declared,
        toolset_profile=compiled.defaults.toolset_profile,
        compiled_spec=compiled.spec,
    )


def test_legacy_preset_tool_manager_helper_is_removed() -> None:
    from modex_agent.multi_agent import template

    assert not hasattr(template, "build_preset_tool_manager")


def test_send_to_agent_fallback_registration_is_removed() -> None:
    """Death grep: the materialize-time baked ``send_to_agent`` fallback
    is gone — the derived entry from the ``subagents`` capability is the
    single registration road (SPEC §5.2)."""
    from modex_agent.multi_agent import template

    assert not hasattr(template, "_register_send_to_agent")


@pytest.mark.asyncio
async def test_materialize_without_any_workspace_source_raises():
    """The workspace-root derivation chain (scope_path → root_provider)
    raises loudly when ALL sources are absent — the old silent
    ``Path(".")`` fallback masked missing workspace wiring."""
    deps, _factory = await _make_deps()
    deps.project_dir = None
    deps.root_provider = None
    deps.scope_path = None
    template = _compiled_template("scout")
    parent = SessionIdFactory().create(agent_name="main")

    with pytest.raises(ValueError, match=r"scope_path or root_provider"):
        await template.materialize(parent_session=parent, invocation_id="inv1", deps=deps)


def test_subagent_workspace_root_prefers_scope_path_over_project_dir():
    """Workspace identity comes from the scope path, NEVER the static
    project dir: production threads both (service project dir + live
    workspace scope path) and the workspace must win for non-home
    workspaces (project_dir-first picked the bot project for every
    workspace — the review's Issue 1)."""
    from modex_agent.multi_agent.template import _subagent_workspace_root

    deps = AgentMaterializeDeps(
        agent_factory=MagicMock(),
        pool=MagicMock(),
        session_factory=SessionIdFactory(),
        broker=MagicMock(),
        tree=MagicMock(spec=SessionTreeManager),
        project_dir=Path("/bot/project/dir"),
        scope_path=ScopePath(workspace_root=Path("/live/workspace"), pool_name="main"),
        root_provider=_StaticRootProvider(Path("/provider/root")),
    )

    assert _subagent_workspace_root(deps) == Path("/live/workspace")

    deps.scope_path = None
    assert _subagent_workspace_root(deps) == Path("/provider/root")

    deps.root_provider = None
    with pytest.raises(ValueError, match=r"scope_path or root_provider"):
        _subagent_workspace_root(deps)


@pytest.mark.asyncio
async def test_materialize_threads_workspace_resources_onto_subagent_chain():
    """The deps-threaded workspace bundle reaches the subagent's assembly
    context (the review's Issue 2: subagent chains carried no workspace
    layer, so workspace-scoped factories like the bot ``kb`` tool could
    never resolve for subagents). Observed by wrapping
    ``assemble_native_agent`` — the native road's single assembly entry —
    and reading ``workspace_resources`` off the context it receives."""
    import modex_agent.plugins.assembly.native_core as native_core_module

    captured: dict[str, object] = {}
    real = native_core_module.assemble_native_agent

    async def _capturing(spec: object, registry: object, inputs: object, **kwargs: object):
        ctx = kwargs.get("ctx")
        captured["resources"] = getattr(ctx, "workspace_resources", "MISSING")
        return await real(spec, registry, inputs, **kwargs)  # type: ignore[arg-type]

    with patch.object(
        native_core_module, "assemble_native_agent", side_effect=_capturing
    ):
        deps, _factory = await _make_deps()
        bundle = object()
        deps.workspace_resources = bundle
        template = _compiled_template("scout")
        parent = SessionIdFactory().create(agent_name="main")
        await template.materialize(
            parent_session=parent, invocation_id="inv1", deps=deps
        )

    assert captured["resources"] is bundle


@pytest.mark.asyncio
async def test_materialize_subagent_send_to_agent_veto_is_respected():
    """Veto regression anchor: a declaration vetoing the derived
    ``send_to_agent`` (``tools: [-send_to_agent]``) materializes a
    subagent WITHOUT the tool. The retired materialize-time fallback used
    to re-register the tool unconditionally, silently defeating the
    user's veto."""
    deps, factory = await _make_deps()
    template = _compiled_template("scout", tools=["-send_to_agent"])
    parent = SessionIdFactory().create(agent_name="main")

    await template.materialize(parent_session=parent, invocation_id="inv1", deps=deps)

    tm = factory.create_agent.call_args.kwargs["tool_manager"]
    assert tm.get_tool("send_to_agent") is None


@pytest.mark.asyncio
async def test_materialize_subagent_only_comm_kind_subagent():
    deps, factory = await _make_deps()
    template = _compiled_template("scout")
    parent = SessionIdFactory().create(agent_name="main")
    await template.materialize(parent_session=parent, invocation_id="inv123", deps=deps)
    call_kwargs = factory.create_agent.call_args
    descriptor = call_kwargs.kwargs.get("descriptor") or call_kwargs.args[0]
    assert descriptor.comm_kind == AgentCommKind.SUBAGENT


@pytest.mark.asyncio
async def test_materialize_subagent_builds_own_tool_manager():
    """Subagents (parent_session set) DO get a materialize-built tool_manager
    + context_manager (session-scoped, preset tools)."""
    deps, factory = await _make_deps()
    template = _compiled_template("scout")
    parent = SessionIdFactory().create(agent_name="main")
    await template.materialize(parent_session=parent, invocation_id="inv1", deps=deps)
    kwargs = factory.create_agent.call_args.kwargs
    assert kwargs["tool_manager"] is not None
    assert kwargs["context_manager"] is not None


@pytest.mark.asyncio
async def test_materialize_parent_none_still_builds_subagent_tool_manager():
    """Design B: materialize is subagent-only. Even with parent_session=None
    (a subagent with no parent context, e.g. a cold-started template), it
    builds a tool_manager from the template rather than passing None through
    to the factory."""
    deps, factory = await _make_deps()
    template = _compiled_template("scout")
    await template.materialize(parent_session=None, invocation_id=None, deps=deps)
    kwargs = factory.create_agent.call_args.kwargs
    assert kwargs["tool_manager"] is not None  # subagent-style, not factory default


@pytest.mark.asyncio
async def test_materialize_skills_veto_omits_only_subagent_resolver() -> None:
    from modex_agent.plugins.defaults.capabilities.skills.supply import (
        build_skills_supply,
    )

    deps, factory = await _make_deps()
    skills_supply = build_skills_supply(
        pool_name="main",
        skill_root_for_agent={"main": []},
    )
    deps.capability_supply = {
        **deps.capability_supply,
        "skills": skills_supply,
    }
    main_resolver = skills_supply.resolver_for("main")
    template = _compiled_template("scout", capabilities={"skills": False})
    parent = SessionIdFactory().create(agent_name="main")

    await template.materialize(parent_session=parent, invocation_id="inv1", deps=deps)

    assert factory.create_agent.call_args.kwargs["skill_resolver"] is None
    assert skills_supply.resolver_for("main") is main_resolver


@pytest.mark.asyncio
async def test_materialize_subagent_inherits_reasoning_effort() -> None:
    """AgentLLMConfig on the subagent descriptor receives llm_reasoning_effort from deps."""
    deps, factory = await _make_deps()
    deps.llm_reasoning_effort = ReasoningEffort.HIGH
    template = _compiled_template("scout")
    parent = SessionIdFactory().create(agent_name="main")
    await template.materialize(parent_session=parent, invocation_id="inv1", deps=deps)
    call_kwargs = factory.create_agent.call_args.kwargs
    descriptor = call_kwargs.get("descriptor") or factory.create_agent.call_args.args[0]
    assert descriptor.llm_config.reasoning_effort == ReasoningEffort.HIGH


@pytest.mark.asyncio
async def test_materialize_subagent_inherits_llm_model_info() -> None:
    """AgentLLMConfig on the subagent descriptor receives llm_model_info from deps.

    Without this threading, the factory gets a descriptor with model_info=None,
    so runtime_services.model_info is None and tools (e.g. ReadFileTool image
    path) degrade to text-only even when the LLM supports IMAGE.
    """
    from modex_agent.core.capabilities import Modality, ModelCapabilities, ModelInfo

    vision_info = ModelInfo(
        model_name="test-vision",
        capabilities=ModelCapabilities(modalities=frozenset({Modality.TEXT, Modality.IMAGE})),
    )
    deps, factory = await _make_deps()
    deps.llm_model_info = vision_info
    template = _compiled_template("scout")
    parent = SessionIdFactory().create(agent_name="main")
    with patch(
        "modex_agent.agents.external.cli_resolver.resolve_modexctl_bin_dir",
        return_value=Path("/fake/bin"),
    ):
        await template.materialize(parent_session=parent, invocation_id="inv1", deps=deps)
    call_kwargs = factory.create_agent.call_args.kwargs
    descriptor = call_kwargs.get("descriptor") or factory.create_agent.call_args.args[0]
    assert descriptor.llm_config.model_info is not None
    assert descriptor.llm_config.model_info is vision_info
    assert descriptor.llm_config.model_info.capabilities.supports(Modality.IMAGE)


@pytest.mark.asyncio
async def test_materialize_subagent_passes_roles_to_descriptor() -> None:
    """T1 data-layer透传: SubagentSpec.roles lands on AgentDescriptor.roles.

    The materialize call constructs the descriptor from the spec; the
    ``roles`` field must round-trip verbatim. Preset values (AgentRole
    members) collapse to their plain string value via StrEnum; custom
    strings are preserved as-is.
    """
    deps, factory = await _make_deps()
    template = _compiled_template("scout", roles=["planner", "custom-role"])
    parent = SessionIdFactory().create(agent_name="main")
    await template.materialize(parent_session=parent, invocation_id="inv1", deps=deps)
    call_kwargs = factory.create_agent.call_args.kwargs
    descriptor = call_kwargs.get("descriptor") or factory.create_agent.call_args.args[0]
    assert descriptor.roles == ["planner", "custom-role"]


@pytest.mark.asyncio
async def test_materialize_subagent_roles_default_empty() -> None:
    """When SubagentSpec omits roles, descriptor.roles defaults to []."""
    deps, factory = await _make_deps()
    template = _compiled_template("scout")
    parent = SessionIdFactory().create(agent_name="main")
    await template.materialize(parent_session=parent, invocation_id="inv1", deps=deps)
    call_kwargs = factory.create_agent.call_args.kwargs
    descriptor = call_kwargs.get("descriptor") or factory.create_agent.call_args.args[0]
    assert descriptor.roles == []


@pytest.mark.asyncio
async def test_materialize_subagent_wires_hooks_to_hook_runner():
    """ADR-0015 D5: the subagent's roster hooks must reach
    pipeline.hook_runner (factory's hooks= param only stores on
    pipeline.hooks, which isn't dispatched). Since the W6 glue
    eradication every subagent hook — the position-default rows and the
    capability-contributed entries alike — is dispatched by the assembly
    core's roster pass onto the runner."""
    from modex_agent.hook.builtin.deliver_retry import DeliverRetryHook
    from modex_agent.hook.builtin.subagent_auto_send import SubagentAutoSendHook
    from modex_agent.hook.runner import HookRunner

    fake_instance = MagicMock()
    fake_instance.pipeline = MagicMock()
    fake_instance.pipeline.hook_runner = HookRunner()
    fake_instance.pipeline._turn_runner.turn_context_builder.runtime_services = AgentRuntimeServices()
    fake_instance.stop = AsyncMock()
    deps, factory = await _make_deps()
    factory.create_agent = AsyncMock(return_value=fake_instance)
    deps.agent_bus = MagicMock()
    template = _compiled_template("scout")
    parent = SessionIdFactory().create(agent_name="main")
    await template.materialize(parent_session=parent, invocation_id="inv1", deps=deps)
    # The roster hooks land on the runner (not just pipeline.hooks):
    # subagent_auto_send (subagents capability contribution) plus the
    # position-default deliver_retry row.
    hooks = [spec.hook for spec in fake_instance.pipeline.hook_runner.hook_specs]
    assert any(isinstance(hook, SubagentAutoSendHook) for hook in hooks)
    assert any(isinstance(hook, DeliverRetryHook) for hook in hooks)


@pytest.mark.asyncio
async def test_materialize_roster_todo_continuation_hook_receives_tree():
    """F3: a subagent roster referencing ``todo_continuation`` materializes
    with the pool's session tree — ``PoolRuntimeDeps.session_tree_manager``
    must be wired from ``deps.tree`` so the roster-dispatched hook (like the
    tree-aware one) never carries ``tree=None``."""
    from modex_agent.hook.builtin.todo_continuation import TodoContinuationHook
    from modex_agent.hook.runner import HookRunner
    from modex_agent.plugins.defaults.capabilities.subagents import SubagentsSupply
    from modex_agent.plugins.defaults.capabilities.todo import TodoSupply

    fake_instance = MagicMock()
    fake_instance.pipeline = MagicMock()
    fake_instance.pipeline.hook_runner = HookRunner()
    fake_instance.pipeline._turn_runner.turn_context_builder.runtime_services = AgentRuntimeServices()
    fake_instance.stop = AsyncMock()
    deps, factory = await _make_deps()
    factory.create_agent = AsyncMock(return_value=fake_instance)
    skills_supply = deps.capability_supply["skills"]
    deps.capability_supply = {
        "todo": TodoSupply(store=MagicMock(name="todo_store")),
        "subagents": SubagentsSupply(service=MagicMock()),
        "skills": skills_supply,
    }
    template = _compiled_template("scout", hooks=["todo_continuation"])
    parent = SessionIdFactory().create(agent_name="main")

    instance = await template.materialize(parent_session=parent, invocation_id="inv1", deps=deps)

    assert instance.pipeline is not None
    assert instance.pipeline.hook_runner is not None
    todo_hooks = [
        spec.hook
        for spec in instance.pipeline.hook_runner.hook_specs
        if isinstance(spec.hook, TodoContinuationHook)
    ]
    assert todo_hooks, "roster todo_continuation hook must be dispatched"
    assert all(hook._tree is deps.tree for hook in todo_hooks)


@pytest.mark.asyncio
async def test_materialize_subagent_registers_cleanup_reorientation() -> None:
    """The subagent's ``todo_reorientation`` memory hook comes from the
    roster→memory-runner dispatch (the ``todo`` capability contributes it)
    — the unconditional template injection died with the supply
    convergence (SPEC §8.2 B2)."""
    from modex_agent.plugins.defaults.capabilities.subagents import SubagentsSupply
    from modex_agent.plugins.defaults.capabilities.todo import TodoSupply

    deps, factory = await _make_deps()
    skills_supply = deps.capability_supply["skills"]
    deps.capability_supply = {
        "todo": TodoSupply(store=MagicMock(name="todo_store")),
        "subagents": SubagentsSupply(service=MagicMock()),
        "skills": skills_supply,
    }
    template = _compiled_template("scout", capabilities={"todo": {}})

    await template.materialize(parent_session=None, invocation_id=None, deps=deps)

    context_manager = factory.create_agent.call_args.kwargs["context_manager"]
    cleanup_hooks = context_manager.memory_system._hook_runner._hooks
    assert isinstance(cleanup_hooks[-1], TodoReorientationHook)
    assert sum(isinstance(hook, TodoReorientationHook) for hook in cleanup_hooks) == 1


# ---------------------------------------------------------------------------
# EXTERNAL subagent dispatch — emitter_factory post-build wiring
# ---------------------------------------------------------------------------


def _empty_component_registry() -> ComponentRegistry:
    return ComponentRegistry()


@pytest.mark.asyncio
async def test_materialize_external_injects_emitter_factory_into_turn_runner():
    """Regression: external subagents bypass pool_builder's
    ``_create_with_emitter`` wrapper (which injects the WebUI emitter for
    react subagents + external main agent). The framework must inject the
    emitter in ``_materialize_external`` so the external subagent's turns
    write transcript events. Without it, ``ExternalTurnRunner`` keeps the
    default ``StreamingAwareEmitter``+``BrokerOutputAdapter`` from
    ``assemble_pipeline`` and turns are invisible in the WebUI history.
    """
    from modex_agent.core.agent import ExecutionStrategyKind, ProviderKind
    from modex_agent.core.session_id import SessionInfo
    from modex_agent.multi_agent.descriptor import AgentDescriptor, AgentInstance
    from modex_agent.multi_agent.execution_strategy import (
        ExecutionStrategy,
        ExecutionStrategyRegistry,
        SubagentAssembly,
    )

    sentinel_emitter_factory = MagicMock(name="webui_emitter_factory")
    fake_turn_runner = MagicMock()
    fake_pipeline = MagicMock()
    fake_pipeline._turn_runner = fake_turn_runner
    fake_instance = AgentInstance(
        descriptor=MagicMock(),
        context_manager=MagicMock(),
        pipeline=fake_pipeline,
    )

    class _StubExternalStrategy(ExecutionStrategy):
        @property
        def name(self) -> str:
            return "external"

        async def assemble_main(self, ctx):  # type: ignore[no-untyped-def]
            raise AssertionError("subagent dispatch must not call assemble_main")

        def validate_pool_spec(self, spec: PoolSpec) -> None:
            return None

        async def assemble_sub(
            self, ctx: AgentContext, deps: AgentMaterializeDeps
        ) -> SubagentAssembly:
            return SubagentAssembly(
                descriptor=AgentDescriptor.model_construct(),
                instance=fake_instance,
            )

    strategy_registry = ExecutionStrategyRegistry()
    strategy_registry.register(_StubExternalStrategy())

    pool = MagicMock()
    pool.register_resident = AsyncMock()
    deps = AgentMaterializeDeps(
        agent_factory=MagicMock(),
        pool=pool,
        session_factory=SessionIdFactory(),
        broker=MagicMock(),
        tree=MagicMock(spec=SessionTreeManager),
        strategy_registry=strategy_registry,
        emitter_factory=sentinel_emitter_factory,
        scope_path=ScopePath(workspace_root=Path("/ws"), pool_name="main"),
        component_registry=_empty_component_registry(),
    )
    template = _compiled_template(
        "coder",
        execution_strategy=ExecutionStrategyKind.EXTERNAL,
        provider_kind=ProviderKind.OPENCODE,
    )
    parent = SessionInfo.from_str("inv1.main")

    await template.materialize(parent_session=parent, invocation_id="inv1", deps=deps)

    fake_turn_runner.set_emitter_factory.assert_called_once_with(sentinel_emitter_factory)
    # deps carries no workspace_manager → the None-guard must skip the call.
    fake_turn_runner.set_pool_context.assert_not_called()


@pytest.mark.asyncio
async def test_materialize_external_injects_pool_context_into_turn_runner():
    """Regression: external subagents bypass the BIZ ``_create_with_emitter``
    wrapper (the only production caller of ``set_pool_context``). The
    framework must wire the workspace manager in ``_materialize_external``
    so ``ExternalTurnRunner`` binds the ACTIVE workspace root during the
    turn — without it, external subagent turns fall back to the pool
    ``project_dir`` workdir instead of the active workspace (wrong under
    multi-live workspaces).
    """
    from modex_agent.core.agent import ExecutionStrategyKind, ProviderKind
    from modex_agent.core.session_id import SessionInfo
    from modex_agent.multi_agent.descriptor import AgentDescriptor, AgentInstance
    from modex_agent.multi_agent.execution_strategy import (
        ExecutionStrategy,
        ExecutionStrategyRegistry,
        SubagentAssembly,
    )

    sentinel_workspace_manager = MagicMock(name="workspace_resolver_cell")
    fake_turn_runner = MagicMock()
    fake_pipeline = MagicMock()
    fake_pipeline._turn_runner = fake_turn_runner
    fake_instance = AgentInstance(
        descriptor=MagicMock(),
        context_manager=MagicMock(),
        pipeline=fake_pipeline,
    )

    class _StubExternalStrategy(ExecutionStrategy):
        @property
        def name(self) -> str:
            return "external"

        async def assemble_main(self, ctx):  # type: ignore[no-untyped-def]
            raise AssertionError("subagent dispatch must not call assemble_main")

        def validate_pool_spec(self, spec: PoolSpec) -> None:
            return None

        async def assemble_sub(
            self, ctx: AgentContext, deps: AgentMaterializeDeps
        ) -> SubagentAssembly:
            return SubagentAssembly(
                descriptor=AgentDescriptor.model_construct(),
                instance=fake_instance,
            )

    strategy_registry = ExecutionStrategyRegistry()
    strategy_registry.register(_StubExternalStrategy())

    pool = MagicMock()
    pool.register_resident = AsyncMock()
    deps = AgentMaterializeDeps(
        agent_factory=MagicMock(),
        pool=pool,
        session_factory=SessionIdFactory(),
        broker=MagicMock(),
        tree=MagicMock(spec=SessionTreeManager),
        strategy_registry=strategy_registry,
        emitter_factory=MagicMock(),
        workspace_manager=sentinel_workspace_manager,
        scope_path=ScopePath(workspace_root=Path("/ws"), pool_name="mypool"),
        component_registry=_empty_component_registry(),
    )
    template = _compiled_template(
        "coder",
        execution_strategy=ExecutionStrategyKind.EXTERNAL,
        provider_kind=ProviderKind.OPENCODE,
    )
    parent = SessionInfo.from_str("inv1.main")

    await template.materialize(parent_session=parent, invocation_id="inv1", deps=deps)

    fake_turn_runner.set_pool_context.assert_called_once_with(
        workspace_manager=sentinel_workspace_manager, pool_name="mypool"
    )


@pytest.mark.asyncio
async def test_materialize_external_skips_emitter_injection_when_deps_emitter_none():
    """No emitter_factory in deps → no set_emitter_factory call; the
    external subagent keeps the default factory from assemble_pipeline."""
    from modex_agent.core.agent import ExecutionStrategyKind, ProviderKind
    from modex_agent.core.session_id import SessionInfo
    from modex_agent.multi_agent.descriptor import AgentDescriptor, AgentInstance
    from modex_agent.multi_agent.execution_strategy import (
        ExecutionStrategy,
        ExecutionStrategyRegistry,
        SubagentAssembly,
    )

    fake_turn_runner = MagicMock()
    fake_pipeline = MagicMock()
    fake_pipeline._turn_runner = fake_turn_runner
    fake_instance = AgentInstance(
        descriptor=MagicMock(),
        context_manager=MagicMock(),
        pipeline=fake_pipeline,
    )

    class _StubExternalStrategy(ExecutionStrategy):
        @property
        def name(self) -> str:
            return "external"

        async def assemble_main(self, ctx):  # type: ignore[no-untyped-def]
            raise AssertionError("subagent dispatch must not call assemble_main")

        def validate_pool_spec(self, spec: PoolSpec) -> None:
            return None

        async def assemble_sub(
            self, ctx: AgentContext, deps: AgentMaterializeDeps
        ) -> SubagentAssembly:
            return SubagentAssembly(
                descriptor=AgentDescriptor.model_construct(),
                instance=fake_instance,
            )

    strategy_registry = ExecutionStrategyRegistry()
    strategy_registry.register(_StubExternalStrategy())

    pool = MagicMock()
    pool.register_resident = AsyncMock()
    deps = AgentMaterializeDeps(
        agent_factory=MagicMock(),
        pool=pool,
        session_factory=SessionIdFactory(),
        broker=MagicMock(),
        tree=MagicMock(spec=SessionTreeManager),
        strategy_registry=strategy_registry,
        emitter_factory=None,
        scope_path=ScopePath(workspace_root=Path("/ws"), pool_name="main"),
        component_registry=_empty_component_registry(),
    )
    template = _compiled_template(
        "coder",
        execution_strategy=ExecutionStrategyKind.EXTERNAL,
        provider_kind=ProviderKind.OPENCODE,
    )
    parent = SessionInfo.from_str("inv1.main")

    await template.materialize(parent_session=parent, invocation_id="inv1", deps=deps)

    fake_turn_runner.set_emitter_factory.assert_not_called()


# ---------------------------------------------------------------------------
# LLM_PROVIDER slot resolution on the sub path (W4.1 C1)
# ---------------------------------------------------------------------------


class _ProbeLLMProvider(CallbackStreamProvider):
    async def chat_stream(
        self,
        messages: list[ChatMessage],
        model: str | None = None,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
        tools: list[dict] | None = None,
        on_content_delta=None,
        on_reasoning_delta=None,
        **kwargs: object,
    ) -> LLMResponse:
        del messages, model, temperature, max_output_tokens, tools, kwargs
        return LLMResponse(content="probe", finish_reason=FinishReason.STOP)

    def get_default_model(self) -> str:
        return "probe-model"


@pytest.mark.asyncio
async def test_materialize_resolves_per_agent_llm_provider_override():
    """A subagent naming an LLM_PROVIDER factory gets that factory's product
    as its agent provider — the resolved instance must actually reach
    ``create_agent`` (T-P2 unit-level twin, asserted on the real ReActAgent
    provider surface ``pipeline.agent._llm_client._provider``)."""
    from pydantic import BaseModel

    from modex_agent.multi_agent.factory import DefaultAgentFactory
    from modex_agent.plugins.abc import ComponentSlot, SimpleFactory

    class _EmptyConfig(BaseModel):
        model_config = {"frozen": True, "extra": "forbid"}

    probe = _ProbeLLMProvider()
    deps, _ = await _make_deps()
    assert deps.component_registry is not None
    deps.component_registry.register(
        ComponentSlot.LLM_PROVIDER, "probe_llm", SimpleFactory(probe, _EmptyConfig)
    )
    # deps.llm_provider carries a DIFFERENT instance: the per-agent override
    # must win over it.
    deps.llm_provider = MagicMock()
    deps.agent_factory = DefaultAgentFactory()
    template = _compiled_template("scout", llm_provider="probe_llm")
    parent = SessionIdFactory().create(agent_name="main")

    instance = await template.materialize(parent_session=parent, invocation_id="inv1", deps=deps)

    assert instance.pipeline is not None
    agent = instance.pipeline.agent
    assert agent._llm_client._provider is probe  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_materialize_reuses_deps_resolved_default_llm_provider():
    """A subagent keeping the pool default provider name reuses the
    deps-assembly-resolved instance instead of re-resolving the name."""
    deps, factory = await _make_deps()
    probe = _ProbeLLMProvider()
    deps.llm_provider = probe
    template = _compiled_template("scout")
    parent = SessionIdFactory().create(agent_name="main")

    await template.materialize(parent_session=parent, invocation_id="inv1", deps=deps)

    kwargs = factory.create_agent.call_args.kwargs
    assert kwargs["llm_provider"] is probe


# ---------------------------------------------------------------------------
# Structural persistent-bash pair on the subagent tool manager
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_materialize_subagent_bash_roster_gets_bash_input_companion():
    """A subagent roster resolving ``bash`` to a PersistentBashTool must
    also carry ``bash_input`` in the SAME tool manager, sharing the shell
    manager (the pair's routing base). Without the companion a
    stdin-waiting command deadlocks the subagent (commands have no default
    timeout, and the stdin-wait notice tells the model to use a tool it
    does not have)."""
    from modex_agent.tools.terminal.persistent_bash import (
        BashInputTool,
        PersistentBashTool,
    )
    from modex_agent.tools.workspace_scoped import WorkspaceScopedTool

    deps, factory = await _make_deps()
    template = _compiled_template("scout")
    parent = SessionIdFactory().create(agent_name="main")
    with patch(
        "modex_agent.plugins.defaults.tools.persistent_bash_supported",
        return_value=True,
    ):
        await template.materialize(parent_session=parent, invocation_id="inv1", deps=deps)

    tm = factory.create_agent.call_args.kwargs["tool_manager"]
    bash = tm.get_tool("bash")
    # The deps carry a root_provider, so assembly wraps the roster bash in
    # a WorkspaceScopedShellTool — the type assertion targets the inner
    # tool (production shape: subagent bash IS workspace-scoped).
    if isinstance(bash, WorkspaceScopedTool):
        bash = bash.inner
    assert isinstance(bash, PersistentBashTool)
    companion = tm.get_tool("bash_input")
    assert isinstance(companion, BashInputTool)
    # v3 routing: both tools route per-conversation through ONE shared
    # PersistentShellManager — the pairing identity production checks
    # (ensure_input_companion).
    assert companion.manager is bash.manager


@pytest.mark.asyncio
async def test_materialize_subagent_bash_without_pty_host_gets_no_companion():
    """Hosts without a POSIX pty resolve the roster bash to the stateless
    SubprocessTool — no companion may register (structural pairing is
    PersistentBashTool-only)."""
    from modex_agent.tools.terminal.subprocess_tool import SubprocessTool
    from modex_agent.tools.workspace_scoped import WorkspaceScopedTool

    deps, factory = await _make_deps()
    template = _compiled_template("scout")
    parent = SessionIdFactory().create(agent_name="main")
    with patch(
        "modex_agent.plugins.defaults.tools.persistent_bash_supported",
        return_value=False,
    ):
        await template.materialize(parent_session=parent, invocation_id="inv1", deps=deps)

    tm = factory.create_agent.call_args.kwargs["tool_manager"]
    bash = tm.get_tool("bash")
    # Workspace-scoped wrapper (see the companion test): assert the inner.
    if isinstance(bash, WorkspaceScopedTool):
        bash = bash.inner
    assert isinstance(bash, SubprocessTool)
    assert tm.get_tool("bash_input") is None


@pytest.mark.asyncio
async def test_materialize_subagent_roster_without_bash_gets_no_companion():
    """A roster with no bash slot registers no bash_input — the companion
    is structural to bash, never an independent roster entry."""
    deps, factory = await _make_deps()
    template = _compiled_template("scout", tools=[])
    parent = SessionIdFactory().create(agent_name="main")
    with patch(
        "modex_agent.plugins.defaults.tools.persistent_bash_supported",
        return_value=True,
    ):
        await template.materialize(parent_session=parent, invocation_id="inv1", deps=deps)

    tm = factory.create_agent.call_args.kwargs["tool_manager"]
    assert tm.get_tool("bash") is None
    assert tm.get_tool("bash_input") is None


# ---------------------------------------------------------------------------
# Delegation boundary (unified-security Ticket 05b)
# ---------------------------------------------------------------------------


def _wired_services(instance: AgentInstance) -> AgentRuntimeServices:
    """The delegation wiring landing site: the turn context builder's
    runtime services after materialization."""
    assert instance.pipeline is not None
    builder = instance.pipeline._turn_runner.turn_context_builder
    assert builder is not None
    services = builder.runtime_services
    assert isinstance(services, AgentRuntimeServices)
    return services


def _classify_ctx() -> AgentContext:
    from modex_agent.memory.history import ListMessageHistory
    from modex_agent.tools.manager import InMemoryToolManager

    return AgentContext(
        system_prompt="test",
        history=ListMessageHistory(),
        tool_manager=InMemoryToolManager(),
        session=SessionInfo.from_str("inv1.scout"),
    )


@pytest.mark.asyncio
async def test_materialize_lands_delegation_snapshot_and_guard_only_approval():
    """PRD #5 anchor: materialization installs the frozen delegation
    snapshot and the guard-only (escalate=False) approval runtime — a
    subagent never owns a card channel."""
    from modex_agent.approval.runtime import ApprovalRuntime
    from modex_agent.sandbox.delegation import DelegationSnapshot
    from modex_agent.sandbox.security_classifier import SecurityClassifier

    deps, factory = await _make_deps()
    template = _compiled_template("scout")
    parent = SessionIdFactory().create(agent_name="main")
    instance = await template.materialize(
        parent_session=parent, invocation_id="inv1", deps=deps
    )

    services = _wired_services(instance)
    snapshot = services.delegation
    assert isinstance(snapshot, DelegationSnapshot)
    assert snapshot.workspace_root == Path("/ws").resolve()
    assert snapshot.depth == 1  # main(0) -> scout(1)
    assert snapshot.source == "delegation"

    approval = services.approval
    assert isinstance(approval, ApprovalRuntime)
    assert isinstance(approval.classifier, SecurityClassifier)
    assert approval.classifier.escalate_enabled is False
    assert services.guard_only_approval is approval


@pytest.mark.asyncio
async def test_materialize_subagent_write_boundary_classification():
    """PRD #5: workspace-external write → HARDLINE (拒绝型 ToolResult via
    ToolNode) with the two-part delegation copy on last_deny_reason;
    in-workspace write → NORMAL."""
    from modex_agent.approval.constants import ApprovalTier
    from modex_agent.core.message import ToolCall

    deps, factory = await _make_deps()
    template = _compiled_template("scout")
    parent = SessionIdFactory().create(agent_name="main")
    instance = await template.materialize(
        parent_session=parent, invocation_id="inv1", deps=deps
    )
    services = _wired_services(instance)
    approval = services.approval
    assert approval is not None
    classifier = approval.classifier
    ctx = _classify_ctx()

    outside = ToolCall(tool_name="write", arguments={"path": "/etc/hosts"}, call_id="c1")
    classification = classifier.classify(outside, ctx)
    assert classification.tier is ApprovalTier.HARDLINE
    reason = classification.deny_reason
    assert reason is not None
    assert reason.startswith(
        "This operation is outside the subagent boundary:"
    )
    assert services.delegation is not None
    assert str(services.delegation.workspace_root) in reason
    assert "Allowed roots:" in reason
    assert (
        "request this operation in the main session." in reason
    )

    inside = ToolCall(tool_name="write", arguments={"path": "src/a.py"}, call_id="c2")
    assert classifier.classify(inside, ctx).tier is ApprovalTier.NORMAL


@pytest.mark.asyncio
async def test_materialize_declared_roots_extend_the_write_envelope():
    """PRD #5: 声明根内写 → NORMAL (the dirs join the envelope)."""
    from modex_agent.approval.constants import ApprovalTier
    from modex_agent.core.message import ToolCall
    from modex_agent.sandbox.settings import ExclusiveConfig, SandboxSettings

    deps, factory = await _make_deps()
    template = _compiled_template(
        "scout",
        sandbox=SandboxSettings(
            exclusive=ExclusiveConfig(writable_roots=[Path("/ws/shared")])
        ),
    )
    parent = SessionIdFactory().create(agent_name="main")
    instance = await template.materialize(
        parent_session=parent, invocation_id="inv1", deps=deps
    )
    services = _wired_services(instance)
    snapshot = services.delegation
    assert snapshot is not None
    assert Path("/ws/shared").resolve() in snapshot.envelope

    approval = services.approval
    assert approval is not None
    shared = ToolCall(
        tool_name="write", arguments={"path": "/ws/shared/lib.ts"}, call_id="c1"
    )
    assert approval.classifier.classify(shared, _classify_ctx()).tier is ApprovalTier.NORMAL


@pytest.mark.asyncio
async def test_materialize_declared_roots_outside_pool_envelope_fails_fast():
    """A declared root escaping the caller envelope aborts materialization
    — a delegation can only narrow, never amplify."""
    from modex_agent.sandbox.settings import ExclusiveConfig, SandboxSettings

    deps, factory = await _make_deps()
    template = _compiled_template(
        "scout",
        sandbox=SandboxSettings(
            exclusive=ExclusiveConfig(writable_roots=[Path("/elsewhere")])
        ),
    )
    parent = SessionIdFactory().create(agent_name="main")

    with pytest.raises(ValueError, match="can only narrow, never amplify"):
        await template.materialize(
            parent_session=parent, invocation_id="inv1", deps=deps
        )


@pytest.mark.asyncio
async def test_materialize_descriptor_carries_declared_depth():
    """The descriptor carries the delegation depth from the declared tree."""
    deps, factory = await _make_deps()
    template = _compiled_template("scout")
    parent = SessionIdFactory().create(agent_name="main")
    await template.materialize(parent_session=parent, invocation_id="inv1", deps=deps)
    call_kwargs = factory.create_agent.call_args.kwargs
    descriptor = call_kwargs.get("descriptor") or factory.create_agent.call_args.args[0]
    assert descriptor.depth == 1


@pytest.mark.asyncio
async def test_materialize_without_root_provider_raises_for_boundary():
    """The delegation boundary needs a workspace-root source — neither a
    live provider nor a scope path is a loud wiring error, never a
    silent no-boundary fallback."""
    deps, factory = await _make_deps()
    deps.root_provider = None
    deps.scope_path = None
    template = _compiled_template("scout")
    parent = SessionIdFactory().create(agent_name="main")

    with pytest.raises(ValueError, match="root_provider"):
        await template.materialize(
            parent_session=parent, invocation_id="inv1", deps=deps
        )


@pytest.mark.asyncio
async def test_materialize_pool_full_access_inherits_to_subagent():
    """An undeclared subagent inherits the caller's permission face — a
    full-access pool yields a full-access subagent (equal, never wider
    than the caller). A DECLARED block still narrows: the second half
    pins a workspace declaration under the full caller."""
    from modex_agent.multi_agent.execution_strategy import PoolAssemblyContext
    from modex_agent.sandbox.settings import ExclusiveConfig, SandboxSettings, WriteSurface

    deps, factory = await _make_deps()
    root = AgentSpec(
        name="main",
        interceptors=["sandbox_guard"],
        interceptor_configs={
            "sandbox_guard": {
                "sandbox": {"backend": "host", "exclusive": {"write_surface": "full"}}
            }
        },
    )
    pool_assembly = MagicMock(spec=PoolAssemblyContext)
    pool_assembly.pool_name = "main"
    pool_assembly.pool_spec = PoolSpec(
        name="main",
        agents=[root, AgentSpec(name="scout", parent="main")],
    )
    pool_assembly.pool_data = None
    pool_assembly.project_dir = Path("/ws")
    pool_assembly.peer_links = ()
    pool_assembly.control_origin = ""
    deps.pool_assembly_ctx = pool_assembly
    template = _compiled_template("scout")
    parent = SessionIdFactory().create(agent_name="main")
    instance = await template.materialize(
        parent_session=parent, invocation_id="inv1", deps=deps
    )

    services = _wired_services(instance)
    snapshot = services.delegation
    assert snapshot is not None
    assert snapshot.backend == "host"
    assert snapshot.enforcement == "none"
    # Inheritance: the undeclared subagent carries the caller's full face.
    assert snapshot.settings.exclusive.write_surface is WriteSurface.FULL

    approval = services.approval
    assert approval is not None
    ctx = _classify_ctx()
    from modex_agent.approval.constants import ApprovalTier
    from modex_agent.core.message import ToolCall

    inside = ToolCall(tool_name="write", arguments={"path": "src/a.py"}, call_id="c1")
    assert approval.classifier.classify(inside, ctx).tier is ApprovalTier.NORMAL
    outside = ToolCall(tool_name="write", arguments={"path": "/etc/hosts"}, call_id="c2")
    assert approval.classifier.classify(outside, ctx).tier is ApprovalTier.NORMAL

    # A DECLARED workspace block narrows even under a full caller.
    narrowed = _compiled_template(
        "scout",
        sandbox=SandboxSettings(exclusive=ExclusiveConfig()),
    )
    instance2 = await narrowed.materialize(
        parent_session=parent, invocation_id="inv2", deps=deps
    )
    services2 = _wired_services(instance2)
    snapshot2 = services2.delegation
    assert snapshot2 is not None
    assert snapshot2.settings.exclusive.write_surface is WriteSurface.WORKSPACE
    assert snapshot2.backend == "host"
    approval2 = services2.approval
    assert approval2 is not None
    assert approval2.classifier.classify(inside, ctx).tier is ApprovalTier.NORMAL
    assert approval2.classifier.classify(outside, ctx).tier is ApprovalTier.HARDLINE
