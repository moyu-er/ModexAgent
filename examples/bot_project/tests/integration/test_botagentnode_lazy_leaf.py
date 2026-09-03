"""Ticket 08 — BotAgentNode relaxation: lazy-leaf graph execution end to end.

Covers the acceptance criteria of
``docs/design/scope-assembly/issues/08-botagentnode-relaxation.md``:

- (a) A graph referencing a never-dispatched lazy leaf executes: the node
  delivers to the session inbox WITHOUT an instance pre-flight check, the
  InboxPoller cold-starts the agent from its template
  (``_materialize_then_turn`` — the same inbox-driven path as session-mode
  dispatch, SPEC §4 axis 3), the turn runs, and results flow back through
  the graph's deliver tool for deliver-capable (NORMAL) nodes.
- (d) Mode neutrality: the materialized instance carries no graph mode
  state — no ``deliver`` tool in its tool manager; the session binding is
  unbound after the turn; a session-mode turn on the same agent coexists.
- (e) The graph executes TWICE with a workspace eviction between the runs
  (``ScopeRegistry.evict_and_release`` direct call — capacity-based LRU
  eviction is dormant, D1). Eviction releases the materialized instance
  with the bundle; the next run re-materializes it from the template.

V10 startup coverage (typo'd graph agent names) is owned by ticket 07's
boot test ``test_boot_v10_missing_graph_agent_fails_loud``
(tests/unit/service/test_scope_declaration_boot.py) — not duplicated here.

The scripted provider swaps only the LLM: the pool, poller, session tree,
template registry, and materialization deps are the real framework objects,
mirroring the production wiring (``create_pool``). The factory wrap wires
the graph turn configuration (binding store + configurators + context
resolver) onto every created instance — the same setters
``_wire_main_pipeline`` applies to the main pipeline in production.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from bot.graph.agent_node import BotAgentNode
from bot.workspace.handle import PoolWorkspaceResources, WorkspaceResolverCell

from modex_agent.core.constants import ExecutionStrategyKind, FinishReason
from modex_agent.core.context import InMemoryContextManager
from modex_agent.core.llm_struct import RuntimeSafetyPolicy
from modex_agent.core.message import ChatMessage
from modex_agent.core.provider import CallbackStreamProvider
from modex_agent.core.session_id import SessionIdFactory
from modex_agent.core.session_registry import InMemorySessionRegistry
from modex_agent.core.session_store import LocalFileSessionStore
from modex_agent.core.types import InputMessage, LLMResponse, ToolCall
from modex_agent.messaging.broker_memory import InMemoryMessageBroker
from modex_agent.multi_agent import SessionRetentionPolicy
from modex_agent.multi_agent.address import AgentAddress
from modex_agent.multi_agent.bus import LocalAgentMessageBus
from modex_agent.multi_agent.comm_kind import AgentCommKind
from modex_agent.multi_agent.context_fork import ContextForkBuilder
from modex_agent.multi_agent.descriptor import (
    AgentDescriptor,
    AgentLLMConfig,
    ContextStrategy,
)
from modex_agent.multi_agent.factory import DefaultAgentFactory
from modex_agent.multi_agent.inbox.consumer import InboxConsumer
from modex_agent.multi_agent.inbox.producer import InboxProducer
from modex_agent.multi_agent.inbox.server_memory import InMemoryInboxServer
from modex_agent.multi_agent.inbox_poller import InboxPoller
from modex_agent.multi_agent.materialize_deps import AgentMaterializeDeps
from modex_agent.multi_agent.pool import AgentPool
from modex_agent.multi_agent.session_tree.manager import SessionTreeManager
from modex_agent.multi_agent.session_tree.session_binding import (
    InMemorySessionBindingStore,
)
from modex_agent.multi_agent.session_tree.store_node import InMemoryTreeNodeStore
from modex_agent.multi_agent.session_tree.store_track import InMemoryMessageTrackStore
from modex_agent.multi_agent.session_tree.store_tree import InMemorySessionTreeStore
from modex_agent.multi_agent.template import AgentTemplate
from modex_agent.multi_agent.template_registry import AgentTemplateRegistry
from modex_agent.pipeline.snapshot import PoolDataSnapshot
from modex_agent.pipeline.turn_context_config import (
    GraphApprovalConfigurator,
    GraphContextBindingConfigurator,
    GraphKnowledgeConfigurator,
    GraphMaxTurnsConfigurator,
    GraphToolConfigurator,
    GraphTopologyConfigurator,
    TurnContextConfigPipeline,
)
from modex_agent.plugins.assembly.spec import AssemblySpec
from modex_agent.scope.spec import AgentSpec
from modex_agent.tools.overflow.local import LocalFileToolOverflowStore
from modex_agent.tools.presets import ToolPreset
from modex_agent.workspace.context import WorkspaceContext
from modex_agent.workspace.factory import ResourceFactory
from modex_agent.workspace.registry import ScopeRegistry
from modex_agent.workspace.scope_path import ScopePath
from modex_agent.workspace.store import GlobalWorkspaceStore
from modex_graph import (
    DefaultGraphState,
    Graph,
    GraphContext,
    GraphEngine,
    GraphNode,
    GraphPayload,
    GraphRuntime,
    NodeTrigger,
    SchedulerKind,
)
from modex_graph.persistence import (
    InMemoryNodeStateStore,
    NullDeliverStoreFactory,
    NullGraphInstanceStore,
)
from modex_graph.persistence.persistence_coordinator import (
    GraphPersistenceCoordinator,
)
from modex_graph.scheduler.bootstrap import BootstrapMode

_POOL_NAME = "review"
_LEAF_AGENT = "office-expert"
_REVIEWER_AGENT = "reviewer"
_LEAF_DESCRIPTION = "Lazy leaf agent declared in the scope tree"
_RESULT_MARKER = "FINAL-ANSWER-FROM-REVIEWER"


# ── Scripted LLM (the only fake in the harness) ──────────────────────────


@dataclass(frozen=True)
class _FakePoolData(PoolDataSnapshot):
    """Minimal concrete PoolDataSnapshot for scope-path resolution tests."""

    context_manager: Any
    turn_store: Any
    trace_store: Any | None = None
    memory_dir: Path | None = None
    runtime_dir: Path | None = None
    pruned_manager: Any | None = None
    experience_dir: Path | None = None



class _ScriptedProvider(CallbackStreamProvider):
    """Queue-driven LLM: each turn pops responses until a no-tool-call one."""

    def __init__(self) -> None:
        self.queue: list[LLMResponse] = []
        self.call_count = 0

    async def chat_stream(
        self,
        messages: list[ChatMessage],
        model: str | None = None,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        on_content_delta=None,
        on_reasoning_delta=None,
        **kwargs: Any,
    ) -> LLMResponse:
        self.call_count += 1
        if self.queue:
            return self.queue.pop(0)
        return LLMResponse(content="done", finish_reason=FinishReason.STOP)

    def get_default_model(self) -> str:
        return "mock-model"


# ── Harness: one workspace build = one full pool runtime ─────────────────


class _PoolBuild:
    """The per-materialize runtime bundle (pool + poller + tree + template)."""

    def __init__(
        self,
        target: Path,
        provider: _ScriptedProvider,
        resolver_cell: WorkspaceResolverCell,
    ) -> None:
        self.broker = InMemoryMessageBroker()
        self.inbox_server = InMemoryInboxServer()
        producer = InboxProducer(server=self.inbox_server)
        consumer = InboxConsumer(server=self.inbox_server)
        self.bus = LocalAgentMessageBus(producer=producer, consumer=consumer)
        self.session_registry = InMemorySessionRegistry()
        self.binding_store = InMemorySessionBindingStore()
        self.provider = provider
        # The current graph run's context — the turn-runner resolver reads it.
        self.graph_ctx_holder: dict[str, GraphContext[DefaultGraphState] | None] = {}

        self.factory = self._build_factory(resolver_cell)
        self.pool = AgentPool(
            broker=self.broker,
            agent_factory=self.factory,
            agent_bus=self.bus,
            inbox_consumer=consumer,
            session_registry=self.session_registry,
            retention=SessionRetentionPolicy(),
        )
        self.poller = InboxPoller(self.pool, interval=0.05)
        self.pool.attach_poller(self.poller)
        self.bus.set_poller(self.poller)

        self.tree_manager = SessionTreeManager(
            tree_store=InMemorySessionTreeStore(),
            node_store=InMemoryTreeNodeStore(),
            track_store=InMemoryMessageTrackStore(),
            bus=self.bus,
            poller=self.poller,
            pool_name=_POOL_NAME,
            workspace_root=str(target),
            session_registry=self.session_registry,
            binding_store=self.binding_store,
        )
        consumer.set_on_consumed(self.tree_manager.on_consumed)
        self.poller.attach_tree_manager(self.tree_manager)

        # The lazy leaf exists ONLY as a template (never dispatched) — the
        # declaration-road shape: boot seeds the registry, the poller
        # materializes on first inbox consumption.
        self.template_registry = AgentTemplateRegistry(
            seeded={
                _POOL_NAME: {
                    _LEAF_AGENT: AgentTemplate(
                        spec=AgentSpec(
                            name=_LEAF_AGENT,
                            description=_LEAF_DESCRIPTION,
                            toolset=ToolPreset.NONE,
                            max_steps=5,
                        ),
                        toolset_profile=ToolPreset.NONE,
                        compiled_spec=self._leaf_compiled_spec(target),
                    )
                }
            }
        )
        self.pool.template_registry = self.template_registry
        self.pool.pool_name = _POOL_NAME
        self.pool.context_fork_builder = ContextForkBuilder()

    async def initialize(self, target: Path) -> None:
        self.pool.materialize_deps = await self._build_materialize_deps(target)

    @classmethod
    async def build(
        cls,
        target: Path,
        provider: _ScriptedProvider,
        resolver_cell: WorkspaceResolverCell,
    ) -> _PoolBuild:
        build = cls(target, provider, resolver_cell)
        await build.initialize(target)
        return build

    def _leaf_compiled_spec(self, target: Path) -> AssemblySpec:
        from modex_agent.plugins.abc import AgentType
        from modex_agent.plugins.assembly.spec import MemoryOverrides
        from modex_agent.workspace.context import WorkspaceContext
        from modex_agent.workspace.paths import WorkspacePaths

        return AssemblySpec(
            agent_type=AgentType.native_sub,
            agent_name=_LEAF_AGENT,
            pool_name=_POOL_NAME,
            description=_LEAF_DESCRIPTION,
            max_iterations=5,
            tools=[],
            hooks=[],
            llm_provider="default",
            system_prompt_provider="file_prompt",
            system_prompt_config={"path": f"agents/{_LEAF_AGENT}.md"},
            memory_overrides=MemoryOverrides(),
            execution_strategy="react",
            workspace_ctx=WorkspaceContext(
                target=target,
                paths=WorkspacePaths(root=target / ".modex"),
                is_home=False,
            ),
        )

    def _build_factory(
        self, resolver_cell: WorkspaceResolverCell
    ) -> DefaultAgentFactory:
        factory = DefaultAgentFactory(default_llm_provider=self.provider)
        original_create = factory.create_agent

        async def _create_with_graph_wiring(*args: Any, **kwargs: Any) -> Any:
            instance = await original_create(*args, **kwargs)
            if instance.pipeline is not None:
                # The same setters production's _wire_main_pipeline applies
                # to the main pipeline (and _create_with_emitter's pool
                # context wrap applies to every created agent) — mirrored
                # here for both the eager reviewer and the lazily
                # materialized leaf.
                turn_runner = instance.pipeline._turn_runner
                turn_runner.set_pool_context(
                    workspace_manager=resolver_cell, pool_name=_POOL_NAME
                )
                builder = turn_runner.turn_context_builder
                if builder is not None:
                    builder.graph_context_resolver = (
                        lambda _gid: self.graph_ctx_holder.get("ctx")
                    )
                    builder.session_binding_store = self.binding_store
                    builder.config_pipeline = TurnContextConfigPipeline([
                        GraphContextBindingConfigurator(),
                        GraphApprovalConfigurator(),
                        GraphMaxTurnsConfigurator(),
                        GraphToolConfigurator(),
                        GraphTopologyConfigurator(),
                        GraphKnowledgeConfigurator(),
                    ])
            return instance

        factory.create_agent = _create_with_graph_wiring  # type: ignore[method-assign]
        return factory

    async def _build_materialize_deps(self, target: Path) -> AgentMaterializeDeps:
        from modex_agent.plugins.defaults import DefaultPlugin
        from modex_agent.plugins.loader import (
            ComponentRegistryLoader,
            PluginDiscoveryConfig,
        )
        from modex_agent.plugins.registry import ComponentRegistry

        registry = ComponentRegistry()
        await ComponentRegistryLoader.load(
            registry,
            PluginDiscoveryConfig(
                bundled_factories=(DefaultPlugin(),),
                project_plugin_paths=(),
            ),
        )
        runtime_dir = target / ".modex" / "runtime_state" / _POOL_NAME
        memory_dir = target / ".modex" / "memory" / _POOL_NAME
        runtime_dir.mkdir(parents=True, exist_ok=True)
        memory_dir.mkdir(parents=True, exist_ok=True)
        manager = MagicMock()
        manager.resolve_workspace.return_value.pool_data.get.return_value = _FakePoolData(
            context_manager=MagicMock(),
            turn_store=MagicMock(),
            runtime_dir=runtime_dir,
            memory_dir=memory_dir,
        )
        return AgentMaterializeDeps(
            agent_factory=self.factory,
            pool=self.pool,
            session_factory=SessionIdFactory(),
            broker=self.broker,
            tree=self.tree_manager,
            safety=RuntimeSafetyPolicy(),
            llm_provider=self.provider,
            project_dir=None,
            agent_bus=self.bus,
            context_fork_builder=ContextForkBuilder(),
            scope_path=ScopePath(workspace_root=target, pool_name=_POOL_NAME),
            workspace_manager=manager,
            component_registry=registry,
        )

    async def register_reviewer(self) -> None:
        """Eagerly register the pool's main agent (NORMAL — deliver-capable)."""
        descriptor = AgentDescriptor(
            address=AgentAddress(name=_REVIEWER_AGENT),
            role_description="Review agent",
            llm_config=AgentLLMConfig(model="mock-model"),
            system_prompt_template="You are the reviewer.",
            max_iterations=5,
            execution_strategy=ExecutionStrategyKind.REACT,
            context_strategy=ContextStrategy.PERSISTENT,
            comm_kind=AgentCommKind.NORMAL,
            safety_policy=RuntimeSafetyPolicy(),
        )
        instance = await self.factory.create_agent(
            descriptor,
            broker=self.broker,
            tool_manager=None,
            skill_resolver=None,
            context_manager=InMemoryContextManager(
                base_system_prompt="You are the reviewer."
            ),
            hooks=[],
        )
        await self.pool.register_resident(descriptor, instance)
        self.pool.start_poller()

    async def close(self) -> None:
        await self.pool.shutdown_all()
        await self.bus.close()
        await self.broker.stop()


class _LazyLeafResourceFactory(ResourceFactory[PoolWorkspaceResources]):
    """Workspace factory: materialize builds a fresh pool build; evict stops it."""

    def __init__(self, provider: _ScriptedProvider, resolver_cell: WorkspaceResolverCell) -> None:
        self.provider = provider
        self.resolver_cell = resolver_cell
        self.builds: list[_PoolBuild] = []

    async def materialize(self, ctx: WorkspaceContext) -> PoolWorkspaceResources:
        build = await _PoolBuild.build(ctx.target, self.provider, self.resolver_cell)
        await build.register_reviewer()
        self.builds.append(build)
        handle = SimpleNamespace(
            pool=build.pool,
            session_binding_store=build.binding_store,
            tree_manager=build.tree_manager,
        )
        ctx.paths.mkdir_skeleton()
        return PoolWorkspaceResources(
            target=ctx.target,
            ctx=ctx,
            overflow_store=LocalFileToolOverflowStore(
                workspace=ctx.paths.overflow_dir
            ),
            session_index_store=LocalFileSessionStore(root=ctx.paths.session_index_dir),
            broker=build.broker,
            pools={_POOL_NAME: handle},  # type: ignore[dict-item]
        )

    async def evict(self, resources: PoolWorkspaceResources) -> None:
        build = next((b for b in self.builds if b.broker is resources.broker), None)
        if build is not None:
            self.builds.remove(build)
            await build.close()


# ── Graph helpers (mirrors tests/integration/test_review_workflow_graph.py) ──


def _make_coordinator(compiled: Any) -> GraphPersistenceCoordinator:
    coord = GraphPersistenceCoordinator(
        graph_instance_id=0,
        instance_store=NullGraphInstanceStore(),
        node_state_store=InMemoryNodeStateStore(0),
        default_deliver_store_factory=NullDeliverStoreFactory(),
    )
    if compiled is not None:
        for node in compiled.nodes.values():
            coord.register_node(node.node_id)
    return coord


async def _run_graph(compiled: Any, build: _PoolBuild, user_input: str) -> Any:
    ctx = GraphContext(
        state=DefaultGraphState(),
        runtime=GraphRuntime(),
        coordinator=_make_coordinator(compiled),
        user_input=GraphPayload(content=user_input),
        scheduler_kind=SchedulerKind.PARALLEL,
        graph_instance_id=0,
    )
    build.graph_ctx_holder["ctx"] = ctx
    engine = GraphEngine(compiled)
    return await engine.run_async(ctx, mode=BootstrapMode.FRESH)


def _compile(g: Graph[DefaultGraphState]) -> Any:
    return g.compile(
        max_iterations=15,
        cycle_detection="off",
        scheduler=SchedulerKind.PARALLEL,
        default_trigger=NodeTrigger.ON_ALL_PREDS,
    )


# ── Fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture
async def lazy_leaf_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncGenerator[
    tuple[
        ScopeRegistry[PoolWorkspaceResources],
        _LazyLeafResourceFactory,
        Path,
        WorkspaceResolverCell,
    ],
    None,
]:
    # template.materialize resolves the modexctl bin dir for
    # NativeEnvInjectionHook — provide a dummy so materialize never depends
    # on a real install.
    fake_bin = tmp_path / "fake_bin"
    fake_bin.mkdir()
    (fake_bin / "modexctl").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    monkeypatch.setenv("MODEXBOT_BIN_DIR", str(fake_bin))

    target = tmp_path / "ws"
    target.mkdir()
    provider = _ScriptedProvider()
    resolver_cell = WorkspaceResolverCell()
    factory = _LazyLeafResourceFactory(provider, resolver_cell)
    store = GlobalWorkspaceStore(home=tmp_path, data_dir_name=".modex")
    registry: ScopeRegistry[PoolWorkspaceResources] = ScopeRegistry(
        home=tmp_path, data_dir_name=".modex", factory=factory, store=store
    )

    ctx = await registry.get_or_open(target)
    resources = await registry.materialize(ctx)
    resolver_cell.set(resources)

    yield registry, factory, target, resolver_cell

    for build in list(factory.builds):
        await build.close()


# ── Tests ────────────────────────────────────────────────────────────────


async def test_lazy_leaf_cold_start_turn_and_eviction_roundtrip(
    lazy_leaf_env: tuple[
        ScopeRegistry[PoolWorkspaceResources],
        _LazyLeafResourceFactory,
        Path,
        WorkspaceResolverCell,
    ],
) -> None:
    """AC (a) relaxation + AC (e) eviction: a graph node referencing a
    never-dispatched lazy leaf delivers without an instance, the poller
    cold-starts the agent from its template, the turn runs — twice, with a
    workspace eviction (releasing the materialized instance) between runs."""
    registry, factory, target, resolver_cell = lazy_leaf_env

    leaf = BotAgentNode(_LEAF_AGENT, _POOL_NAME, resolver_cell)
    g: Graph[DefaultGraphState] = Graph("review_cycle_lazy")
    g.add_node("leaf", leaf)
    g.add_edge(GraphNode.START, "leaf")
    g.add_edge("leaf", GraphNode.END)
    compiled = _compile(g)

    # ── Run 1: no instance exists — the old pre-flight RuntimeError path is
    # gone; the deliver goes through and the poller materializes the leaf.
    build1 = factory.builds[0]
    assert build1.pool.get(_LEAF_AGENT) is None, "leaf must start un-materialized"
    calls_before = factory.provider.call_count

    result1 = await _run_graph(compiled, build1, "summarize the report")

    assert build1.pool.get(_LEAF_AGENT) is not None, (
        "InboxPoller did not cold-start the lazy leaf from its template"
    )
    assert factory.provider.call_count > calls_before, "leaf turn never ran"
    assert result1 is not None  # engine returned without raising

    # Mode neutrality (AC d): instance products carry no graph mode state —
    # the deliver tool lives in per-turn artifacts only, never in the
    # materialized instance's tool manager.
    leaf_instance = build1.pool.get(_LEAF_AGENT)
    assert leaf_instance is not None and leaf_instance.pipeline is not None
    tool_manager = leaf_instance.pipeline._turn_runner.tool_manager
    assert tool_manager is not None, "materialized leaf has no tool manager"
    assert tool_manager.get_tool("deliver") is None, (
        "deliver tool leaked into the materialized instance — mode-specific "
        "state must stay per-turn (SPEC §4 axis 3 hard contract)"
    )
    assert leaf_instance.descriptor.comm_kind == AgentCommKind.SUBAGENT

    # ── Eviction (AC e): evict_and_release drops the bundle — the
    # materialized instance goes with it.
    await registry.evict_and_release(target)
    assert registry.materialized_count() == 0

    # ── Re-materialize: a fresh pool build with NO leaf instance.
    ctx = await registry.get_or_open(target)
    resources2 = await registry.materialize(ctx)
    resolver_cell.set(resources2)
    build2 = factory.builds[0]
    assert build2 is not build1
    assert build2.pool.get(_LEAF_AGENT) is None, (
        "eviction must release the materialized instance with the bundle"
    )

    # ── Run 2: same compiled graph, same node — the leaf cold-starts again.
    calls_before_run2 = factory.provider.call_count
    result2 = await _run_graph(compiled, build2, "summarize the report again")

    assert build2.pool.get(_LEAF_AGENT) is not None, (
        "leaf did not re-materialize after eviction"
    )
    assert factory.provider.call_count > calls_before_run2
    assert result2 is not None


async def test_deliver_capable_node_result_flows_back(
    lazy_leaf_env: tuple[
        ScopeRegistry[PoolWorkspaceResources],
        _LazyLeafResourceFactory,
        Path,
        WorkspaceResolverCell,
    ],
) -> None:
    """AC (a) result flow: through the same harness, a NORMAL (main) graph
    node runs its graph-configured turn and its deliver tool call flows back
    through END into the graph result — the control isolating the lazy
    subagent leaf's silent completion to the comm-kind configurator gate,
    not to a broken harness."""
    _registry, factory, _target, resolver_cell = lazy_leaf_env
    build = factory.builds[0]

    reviewer = BotAgentNode(_REVIEWER_AGENT, _POOL_NAME, resolver_cell)
    g: Graph[DefaultGraphState] = Graph("review_flow")
    g.add_node("reviewer", reviewer)
    g.add_edge(GraphNode.START, "reviewer")
    g.add_edge("reviewer", GraphNode.END)
    compiled = _compile(g)

    factory.provider.queue = [
        LLMResponse(
            content="",
            tool_calls=[
                ToolCall(
                    tool_name="deliver",
                    arguments={"target": GraphNode.END, "content": _RESULT_MARKER},
                    call_id="c1",
                )
            ],
        ),
        LLMResponse(content="reviewer done", finish_reason=FinishReason.STOP),
    ]

    result = await _run_graph(compiled, build, "review the change")

    assert result is not None
    assert result.result is not None
    assert any(
        _RESULT_MARKER in (p.content or "") for p in result.result
    ), f"deliver-tool result did not flow back through END: {result.result}"

    # The eager reviewer instance stays mode-neutral too: no deliver tool
    # registered on the instance's own tool manager.
    reviewer_instance = build.pool.get(_REVIEWER_AGENT)
    assert reviewer_instance is not None and reviewer_instance.pipeline is not None
    tool_manager = reviewer_instance.pipeline._turn_runner.tool_manager
    assert tool_manager is not None
    assert tool_manager.get_tool("deliver") is None


async def test_graph_turn_and_session_turn_coexist(
    lazy_leaf_env: tuple[
        ScopeRegistry[PoolWorkspaceResources],
        _LazyLeafResourceFactory,
        Path,
        WorkspaceResolverCell,
    ],
) -> None:
    """AC (d): graph turns and session turns for the SAME agent coexist
    without cross-talk — after a graph turn, a session-mode turn (direct
    inbox submit) runs on the same materialized instance with no graph
    binding left over."""
    _registry, factory, _target, resolver_cell = lazy_leaf_env
    build = factory.builds[0]

    leaf = BotAgentNode(_LEAF_AGENT, _POOL_NAME, resolver_cell)
    g: Graph[DefaultGraphState] = Graph("graph_then_session")
    g.add_node("leaf", leaf)
    g.add_edge(GraphNode.START, "leaf")
    g.add_edge("leaf", GraphNode.END)
    compiled = _compile(g)

    await _run_graph(compiled, build, "graph turn input")
    leaf_instance = build.pool.get(_LEAF_AGENT)
    assert leaf_instance is not None, "graph turn did not materialize the leaf"
    # The graph turn unbound its session binding — session-mode turns on
    # other sessions see no graph state.
    assert leaf._session is not None
    assert build.binding_store.get(leaf._session.session_id) is None

    # Session-mode turn: a fresh session on the SAME agent, driven the same
    # way every human DM is (submit_input → poller → turn).
    session = SessionIdFactory().create(_LEAF_AGENT, external_id="conv-1")
    await build.session_registry.register(session)
    message = InputMessage(content="session turn input", session=session)
    await build.pool.submit_input(session.session_id, message)

    calls_before = factory.provider.call_count
    deadline = asyncio.get_running_loop().time() + 10.0
    while asyncio.get_running_loop().time() < deadline:
        if factory.provider.call_count > calls_before:
            break
        await asyncio.sleep(0.05)
    assert factory.provider.call_count > calls_before, (
        "session-mode turn never ran on the same agent after the graph turn"
    )
    # No graph binding ever existed for the session-mode session.
    assert build.binding_store.get(session.session_id) is None
