"""Tests for AgentTemplate.materialize — the single construction path."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modex_agent.core.constants import FinishReason, ReasoningEffort
from modex_agent.core.llm_struct import RuntimeSafetyPolicy
from modex_agent.core.message import ChatMessage
from modex_agent.core.provider import LLMProvider
from modex_agent.core.session_id import SessionIdFactory
from modex_agent.core.types import LLMResponse
from modex_agent.memory.cleanup_hooks import TodoReorientationHook
from modex_agent.multi_agent.comm_kind import AgentCommKind
from modex_agent.multi_agent.context_fork import ContextForkBuilder
from modex_agent.multi_agent.materialize_deps import AgentMaterializeDeps
from modex_agent.multi_agent.session_tree.manager import SessionTreeManager
from modex_agent.multi_agent.template import AgentTemplate
from modex_agent.plugins.registry import ComponentRegistry
from modex_agent.scope.compiler import compile_scope
from modex_agent.scope.spec import AgentSpec, PoolSpec, ScopeKind, ScopeSpec
from modex_agent.workspace.context import WorkspaceContext
from modex_agent.workspace.paths import WorkspacePaths
from modex_agent.workspace.scope_path import ScopePath


async def _make_deps() -> tuple[AgentMaterializeDeps, MagicMock]:
    """Build deps with a mocked agent_factory + pool."""
    fake_instance = MagicMock()
    fake_instance.pipeline = MagicMock()
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
        component_registry=registry,
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
    declared = AgentSpec(name=name, parent="main", **agent_kwargs)
    spec = ScopeSpec(
        kind=ScopeKind.POOL,
        pool=PoolSpec(name="main", agents=[AgentSpec(name="main"), declared]),
    )
    compilation = compile_scope(spec, workspace_ctx=_workspace_ctx())
    compiled = next(a for a in compilation.agents if a.provenance.agent == name)
    return AgentTemplate(
        spec=declared,
        toolset_profile=compiled.defaults.toolset_profile,
        compiled_spec=compiled.spec,
    )


def test_legacy_preset_tool_manager_helper_is_removed() -> None:
    from modex_agent.multi_agent import template

    assert not hasattr(template, "build_preset_tool_manager")


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
    """ADR-0015 D5: SubagentAutoSendHook must reach pipeline.hook_runner
    (factory's hooks= param only stores on pipeline.hooks, which isn't
    dispatched). materialize adds hooks to hook_runner post-creation."""
    fake_instance = MagicMock()
    fake_instance.pipeline = MagicMock()
    fake_instance.pipeline.hook_runner = MagicMock()  # truthy → add() is callable
    fake_instance.stop = AsyncMock()
    deps, factory = await _make_deps()
    factory.create_agent = AsyncMock(return_value=fake_instance)
    deps.agent_bus = MagicMock()
    template = _compiled_template("scout")
    parent = SessionIdFactory().create(agent_name="main")
    await template.materialize(parent_session=parent, invocation_id="inv1", deps=deps)
    # SubagentAutoSendHook must be added to hook_runner (not just pipeline.hooks)
    assert fake_instance.pipeline.hook_runner.add.call_count >= 1


@pytest.mark.asyncio
async def test_materialize_roster_todo_continuation_hook_receives_tree():
    """F3: a subagent roster referencing ``todo_continuation`` materializes
    with the pool's session tree — ``PoolRuntimeDeps.session_tree_manager``
    must be wired from ``deps.tree`` so the roster-dispatched hook (like the
    tree-aware one) never carries ``tree=None``."""
    from modex_agent.hook.builtin.todo_continuation import TodoContinuationHook
    from modex_agent.hook.runner import HookRunner

    fake_instance = MagicMock()
    fake_instance.pipeline = MagicMock()
    fake_instance.pipeline.hook_runner = HookRunner()
    fake_instance.stop = AsyncMock()
    deps, factory = await _make_deps()
    factory.create_agent = AsyncMock(return_value=fake_instance)
    template = _compiled_template("scout", hooks=["todo_continuation"])
    parent = SessionIdFactory().create(agent_name="main")

    instance = await template.materialize(
        parent_session=parent, invocation_id="inv1", deps=deps
    )

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
    deps, factory = await _make_deps()
    template = _compiled_template("scout")

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
    from modex_agent.core.constants import ExecutionStrategyKind, ProviderKind
    from modex_agent.core.session_id import SessionInfo
    from modex_agent.multi_agent.descriptor import AgentDescriptor, AgentInstance
    from modex_agent.multi_agent.execution_strategy import (
        ExecutionStrategy,
        ExecutionStrategyRegistry,
        SubagentAssembly,
    )
    from modex_agent.plugins.assembly.context import AgentContext
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
    from modex_agent.core.constants import ExecutionStrategyKind, ProviderKind
    from modex_agent.core.session_id import SessionInfo
    from modex_agent.multi_agent.descriptor import AgentDescriptor, AgentInstance
    from modex_agent.multi_agent.execution_strategy import (
        ExecutionStrategy,
        ExecutionStrategyRegistry,
        SubagentAssembly,
    )
    from modex_agent.plugins.assembly.context import AgentContext
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
    from modex_agent.core.constants import ExecutionStrategyKind, ProviderKind
    from modex_agent.core.session_id import SessionInfo
    from modex_agent.multi_agent.descriptor import AgentDescriptor, AgentInstance
    from modex_agent.multi_agent.execution_strategy import (
        ExecutionStrategy,
        ExecutionStrategyRegistry,
        SubagentAssembly,
    )
    from modex_agent.plugins.assembly.context import AgentContext
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


class _ProbeLLMProvider(LLMProvider):
    async def chat(
        self,
        messages: list[ChatMessage],
        model: str | None = None,
        temperature: float = 0.7,
        max_output_tokens: int | None = None,
        tools: list[dict] | None = None,
        **kwargs: object,
    ) -> LLMResponse:
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

    deps, factory = await _make_deps()
    template = _compiled_template("scout")
    parent = SessionIdFactory().create(agent_name="main")
    with patch(
        "modex_agent.plugins.defaults.tools.persistent_bash_supported",
        return_value=False,
    ):
        await template.materialize(parent_session=parent, invocation_id="inv1", deps=deps)

    tm = factory.create_agent.call_args.kwargs["tool_manager"]
    assert isinstance(tm.get_tool("bash"), SubprocessTool)
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
