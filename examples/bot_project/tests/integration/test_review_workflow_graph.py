# ruff: noqa: ANN401
"""Unit + integration tests for the designer→implementer→reviewer graph workflow.

Part 1 (unit): Topology validation — graph compiles under ParallelScheduler,
cycle is allowed, routing logic works with stub Node stand-ins.

Part 2 (integration): E2E with real StepFun LLM — builds real agents, runs
the graph, verifies the workflow completes.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from modex_agent.core.session_id import SessionInfo
from modex_agent.core.session_registry import InMemorySessionRegistry
from modex_agent.messaging.broker_memory import InMemoryMessageBroker
from modex_agent.multi_agent.bus import LocalAgentMessageBus
from modex_agent.multi_agent.descriptor import AgentInstance
from modex_agent.multi_agent.factory import DefaultAgentFactory
from modex_agent.multi_agent.inbox.consumer import InboxConsumer
from modex_agent.multi_agent.inbox.producer import InboxProducer
from modex_agent.multi_agent.inbox.server_memory import InMemoryInboxServer
from modex_agent.multi_agent.inbox_poller import InboxPoller
from modex_agent.multi_agent.pool import AgentPool
from modex_agent.multi_agent.session_tree.manager import SessionTreeManager
from modex_agent.multi_agent.session_tree.session_binding import (
    InMemorySessionBindingStore,
)
from modex_agent.multi_agent.session_tree.store_node import InMemoryTreeNodeStore
from modex_agent.multi_agent.session_tree.store_track import InMemoryMessageTrackStore
from modex_agent.multi_agent.session_tree.store_tree import InMemorySessionTreeStore
from modex_agent.pipeline.turn_context_config import (
    GraphApprovalConfigurator,
    GraphContextBindingConfigurator,
    GraphKnowledgeConfigurator,
    GraphMaxTurnsConfigurator,
    GraphToolConfigurator,
    GraphTopologyConfigurator,
    TurnContextConfigPipeline,
)
from modex_agent.workspace.paths import WorkspacePaths
from modex_graph import (
    DefaultGraphState,
    Graph,
    GraphContext,
    GraphEngine,
    GraphNode,
    GraphPayload,
    GraphRuntime,
    IntegratedInput,
    Node,
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

_BOT_PROJECT = Path(__file__).resolve().parents[2]
if str(_BOT_PROJECT) not in sys.path:
    sys.path.insert(0, str(_BOT_PROJECT))

_FIXTURES = Path(__file__).resolve().parent / "fixtures"

from bot.graph.agent_node import BotAgentNode  # noqa: E402

# ── Test helpers ────────────────────────────────────────────────────────


def _make_coordinator(
    compiled: Any = None,
) -> GraphPersistenceCoordinator:
    """Build a Null-strategy coordinator, optionally registering compiled nodes."""
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


def _make_ctx(
    coordinator: GraphPersistenceCoordinator | None = None,
    user_input: str = "Write a Python function that checks if a string is a palindrome.",
) -> GraphContext[DefaultGraphState]:
    """Build a minimal parallel-scheduler context."""
    return GraphContext(
        state=DefaultGraphState(),
        runtime=GraphRuntime(),
        coordinator=coordinator or _make_coordinator(),
        user_input=GraphPayload(content=user_input),
        scheduler_kind=SchedulerKind.PARALLEL,
        graph_instance_id=0,
    )


class _ReviewPoolRuntime:
    def __init__(
        self,
        broker: InMemoryMessageBroker,
        factory: DefaultAgentFactory,
        session_registry: InMemorySessionRegistry,
    ) -> None:
        inbox_server = InMemoryInboxServer()
        producer = InboxProducer(server=inbox_server)
        consumer = InboxConsumer(server=inbox_server)
        bus = LocalAgentMessageBus(producer=producer, consumer=consumer)

        self.pool = AgentPool(
            broker=broker,
            agent_factory=factory,
            agent_bus=bus,
            inbox_consumer=consumer,
            session_registry=session_registry,
        )
        poller = InboxPoller(self.pool, interval=0.05)
        self.pool.attach_poller(poller)
        bus.set_poller(poller)

        self.session_binding_store = InMemorySessionBindingStore()
        self.tree_manager = SessionTreeManager(
            tree_store=InMemorySessionTreeStore(),
            node_store=InMemoryTreeNodeStore(),
            track_store=InMemoryMessageTrackStore(),
            bus=bus,
            poller=poller,
            pool_name="review",
            workspace_root=str(_BOT_PROJECT),
            session_registry=session_registry,
            binding_store=self.session_binding_store,
        )
        consumer.set_on_consumed(self.tree_manager.on_consumed)
        poller.attach_tree_manager(self.tree_manager)
        self.pool.tree = self.tree_manager

        self.pool_data = None
        self._broker = broker
        self._bus = bus

    async def register(self, instances: dict[str, AgentInstance]) -> None:
        for instance in instances.values():
            await self.pool.register_resident(instance.descriptor, instance)
        self.pool.start_poller()

    def bind_graph_context(self, ctx: GraphContext[DefaultGraphState]) -> None:
        def resolve_graph_context(_: int) -> GraphContext[DefaultGraphState]:
            return ctx

        for instance in self.pool.iter_instances():
            assert instance.pipeline is not None
            builder = instance.pipeline._turn_context_builder
            assert builder is not None
            builder.graph_context_resolver = resolve_graph_context
            builder.session_binding_store = self.session_binding_store
            builder.config_pipeline = TurnContextConfigPipeline([
                GraphContextBindingConfigurator(),
                GraphApprovalConfigurator(),
                GraphMaxTurnsConfigurator(),
                GraphToolConfigurator(),
                GraphTopologyConfigurator(),
                GraphKnowledgeConfigurator(),
            ])

    async def close(self) -> None:
        await self.pool.shutdown_all()
        await self._bus.close()
        await self._broker.stop()


# ── Stub nodes for topology testing ─────────────────────────────────────


class _StubNode(Node[DefaultGraphState]):
    """Delivers to a fixed target."""

    def __init__(self, target: str, label: str = "") -> None:
        super().__init__()
        self._target = target
        self._label = label

    async def execute(
        self, ctx: GraphContext[DefaultGraphState], integrated_input: IntegratedInput
    ) -> None:
        self.deliver(GraphPayload(content=f"{self._label} output"), self._target, ctx)


class _ReviewApproveNode(Node[DefaultGraphState]):
    """Reviewer that approves → delivers to END."""

    async def execute(
        self, ctx: GraphContext[DefaultGraphState], integrated_input: IntegratedInput
    ) -> None:
        self.deliver(GraphPayload(content="APPROVED"), GraphNode.END, ctx)


class _ReviewApproveAfterRejectNode(Node[DefaultGraphState]):
    """Reviewer that rejects once, then approves on second review."""

    def __init__(self) -> None:
        super().__init__()
        self.call_count = 0

    async def execute(
        self, ctx: GraphContext[DefaultGraphState], integrated_input: IntegratedInput
    ) -> None:
        self.call_count += 1
        if self.call_count == 1:
            self.deliver(
                GraphPayload(content="NEEDS_REVISION: fix typo"), "implementer", ctx
            )
        else:
            self.deliver(GraphPayload(content="APPROVED"), GraphNode.END, ctx)


def _build_graph(reviewer: Node[DefaultGraphState]) -> Graph[DefaultGraphState]:
    """Build the review workflow topology imperatively."""
    g: Graph[DefaultGraphState] = Graph()
    g.add_node("designer", _StubNode(target="implementer", label="designer"))
    g.add_node("implementer", _StubNode(target="reviewer", label="implementer"))
    g.add_node("reviewer", reviewer)
    g.add_edge(GraphNode.START, "designer")
    g.add_edge("designer", "implementer")
    g.add_edge("implementer", "reviewer")
    g.add_edge("reviewer", "implementer")
    g.add_edge("reviewer", GraphNode.END)
    return g


def _compile(g: Graph[DefaultGraphState]) -> Any:
    return g.compile(
        max_iterations=50,
        cycle_detection="off",
        scheduler=SchedulerKind.PARALLEL,
        default_trigger=NodeTrigger.ON_ALL_PREDS,
    )


# ── Part 1: Spec loading ────────────────────────────────────────────────


class TestSpecLoads:
    """Verify the YAML spec loads into a valid GraphSpec."""

    def test_spec_file_exists(self) -> None:
        assert (_FIXTURES / "graphs" / "review_workflow.yml").exists()

    def test_spec_loads_and_validates(self) -> None:
        import yaml

        from modex_graph import GraphSpec, NodeTrigger, SchedulerKind

        raw = yaml.safe_load(
            (_FIXTURES / "graphs" / "review_workflow.yml").read_text(
                encoding="utf-8"
            )
        )
        spec = GraphSpec.model_validate(raw)
        assert spec.name == "review_workflow"
        assert spec.scheduler == SchedulerKind.PARALLEL
        assert spec.default_trigger == NodeTrigger.ON_ALL_PREDS
        assert spec.max_iterations == 50
        assert len(spec.nodes) == 3
        assert len(spec.edges) == 5

    def test_spec_has_cycle_and_exit(self) -> None:
        import yaml

        from modex_graph import GraphSpec

        raw = yaml.safe_load(
            (_FIXTURES / "graphs" / "review_workflow.yml").read_text(
                encoding="utf-8"
            )
        )
        spec = GraphSpec.model_validate(raw)
        edges = [(e.source, e.target) for e in spec.edges]
        assert ("__start__", "designer") in edges
        assert ("designer", "implementer") in edges
        assert ("implementer", "reviewer") in edges
        assert ("reviewer", "implementer") in edges  # cycle
        assert ("reviewer", "__end__") in edges  # exit


# ── Part 2: Topology compilation ────────────────────────────────────────


class TestTopologyCompiles:
    """Verify the graph compiles under ParallelScheduler."""

    def test_compiles_with_cycle_off(self) -> None:
        g = _build_graph(_ReviewApproveNode())
        compiled = _compile(g)
        assert compiled.scheduler == SchedulerKind.PARALLEL
        assert compiled.entry_node == GraphNode.START
        assert "designer" in compiled.nodes
        assert "implementer" in compiled.nodes
        assert "reviewer" in compiled.nodes

    def test_compiles_with_cycle_warn(self) -> None:
        """Default cycle_detection='warn' warns but does not raise."""
        g = _build_graph(_ReviewApproveNode())
        with pytest.warns(UserWarning, match="cycles"):
            compiled = g.compile(
                max_iterations=50,
                scheduler=SchedulerKind.PARALLEL,
                default_trigger=NodeTrigger.ON_ALL_PREDS,
            )
        assert compiled is not None


# ── Part 3: Execution — approve path ────────────────────────────────────


class TestApprovePath:
    """Reviewer approves → graph terminates via END."""

    async def test_approve_terminates(self) -> None:
        g = _build_graph(_ReviewApproveNode())
        compiled = _compile(g)
        coord = _make_coordinator(compiled)
        ctx = _make_ctx(coord)
        engine = GraphEngine(compiled)
        result = await engine.run_async(ctx, mode=BootstrapMode.FRESH)
        assert result is not None
        assert result.result is not None
        assert len(result.result) > 0


# ── Part 4: Execution — reject-then-approve loop ────────────────────────


class TestRejectApproveLoop:
    """Reviewer rejects once, then approves — verifies the cycle works."""

    async def test_loop_then_terminate(self) -> None:
        reviewer = _ReviewApproveAfterRejectNode()
        g = _build_graph(reviewer)
        compiled = _compile(g)
        coord = _make_coordinator(compiled)
        ctx = _make_ctx(coord)
        engine = GraphEngine(compiled)
        result = await engine.run_async(ctx, mode=BootstrapMode.FRESH)
        assert result is not None
        assert reviewer.call_count == 2
        assert result.result is not None
        assert len(result.result) > 0


# ── Part 5: Pool config validation ──────────────────────────────────────


class TestPoolConfig:
    """Verify the review pool config files exist and are valid."""

    def test_pool_yml_exists(self) -> None:
        assert (_FIXTURES / "pools" / "review" / "pool.yml").exists()

    def test_templates_exist(self) -> None:
        templates_dir = _FIXTURES / "pools" / "review" / "templates"
        assert (templates_dir / "implementer.yml").exists()
        assert (templates_dir / "reviewer.yml").exists()

    def test_agent_prompts_exist(self) -> None:
        agents_dir = _FIXTURES / "agents"
        assert (agents_dir / "designer.md").exists()
        assert (agents_dir / "implementer.md").exists()
        assert (agents_dir / "reviewer.md").exists()


# ── Part 6: Integration — E2E with StepFun ──────────────────────────────


@pytest.mark.integration
class TestE2EStepFun:
    """End-to-end test with real LLM (configured via tests/integration/.env).

    Builds real agents, runs the full 3-node graph, verifies the workflow
    completes. Skipped when .env is absent.
    """

    async def test_workflow_completes(
        self, tmp_path: Path, e2e_model_config: Any
    ) -> None:
        from bot.graph.agent_node import BotAgentNode, SessionStrategy

        from modex_agent.core.capabilities import Modality, ModelCapabilities, ModelInfo
        from modex_agent.core.constants import ExecutionStrategyKind
        from modex_agent.core.llm_struct import (
            LLMTimeoutPolicy,
            RuntimeSafetyPolicy,
            TurnTimeoutPolicy,
        )
        from modex_agent.core.scope import MemoryAgentRole
        from modex_agent.ioc.configs.llm import LLMConfig
        from modex_agent.ioc.factories.descriptors import build_session_only_memory
        from modex_agent.ioc.factories.llm import create_llm_provider
        from modex_agent.multi_agent.address import AgentAddress
        from modex_agent.multi_agent.comm_kind import AgentCommKind
        from modex_agent.multi_agent.descriptor import (
            AgentDescriptor,
            AgentLLMConfig,
        )

        mc = e2e_model_config
        provider = create_llm_provider(
            LLMConfig(
                model=mc.model,
                api_key=mc.api_key,
                base_url=mc.base_url,
                temperature=mc.temperature,
                max_output_tokens=mc.max_output_tokens,
                reasoning_effort=mc.reasoning_effort,
            )
        )
        caps = ModelInfo(
            model_name=mc.model,
            capabilities=ModelCapabilities(modalities=frozenset({Modality.TEXT})),
        )
        safety = RuntimeSafetyPolicy(
            llm=LLMTimeoutPolicy(
                request_timeout_seconds=45.0,
                stream_idle_timeout_seconds=90.0,
                framework_max_retries=1,
                retry_backoff_seconds=(2.0, 8.0),
            ),
            turn=TurnTimeoutPolicy(
                dispatch_timeout_seconds=420.0,
                hook_timeout_seconds=10.0,
            ),
        )

        broker = InMemoryMessageBroker()
        await broker.start()
        factory = DefaultAgentFactory(default_llm_provider=provider)

        agents_dir = _FIXTURES / "agents"
        instances: dict[str, Any] = {}
        for name in ("designer", "implementer", "reviewer"):
            prompt = (agents_dir / f"{name}.md").read_text(encoding="utf-8")
            memory_ctx = build_session_only_memory(
                cfg=None,
                workspace=tmp_path / "memory" / name,
                agent_id=name,
                agent_role=MemoryAgentRole.SUBAGENT,
                system_prompt=prompt,
                pruned_manager=None,
            )
            descriptor = AgentDescriptor(
                address=AgentAddress(name=name),
                role_description=f"{name.capitalize()} agent",
                llm_config=AgentLLMConfig(
                    model=mc.model,
                    temperature=mc.temperature,
                    max_output_tokens=mc.max_output_tokens,
                    reasoning_effort=mc.reasoning_effort,
                    model_info=caps,
                ),
                system_prompt_template=prompt,
                max_iterations=30,
                execution_strategy=ExecutionStrategyKind.REACT,
                context_strategy="persistent",
                comm_kind=AgentCommKind.NORMAL,
                safety_policy=safety,
            )
            instance = await factory.create_agent(
                descriptor,
                broker=broker,
                tool_manager=None,
                skill_resolver=None,
                context_manager=memory_ctx,
                hooks=[],
            )
            instances[name] = instance

        session_registry = InMemorySessionRegistry()

        runtime = _ReviewPoolRuntime(broker, factory, session_registry)
        await runtime.register(instances)

        class _FakeWS:
            def __init__(self) -> None:
                self.pools = {"review": runtime}
                self.pool_data: dict[str, Any] = {}
                self.ctx = SimpleNamespace(
                    paths=WorkspacePaths(root=tmp_path / ".modex")
                )

        class _FakeResolver:
            def resolve_workspace(self) -> Any:
                return _FakeWS()

        resolver: Any = _FakeResolver()
        designer = BotAgentNode(
            "designer", "review", resolver,
            session_strategy=SessionStrategy.PER_INVOCATION,
        )
        implementer = BotAgentNode(
            "implementer", "review", resolver,
            session_strategy=SessionStrategy.PER_INVOCATION,
        )
        reviewer = BotAgentNode(
            "reviewer", "review", resolver,
            session_strategy=SessionStrategy.PER_INVOCATION,
        )

        g: Graph[DefaultGraphState] = Graph()
        g.add_node("designer", designer)
        g.add_node("implementer", implementer)
        g.add_node("reviewer", reviewer)
        g.add_edge(GraphNode.START, "designer")
        g.add_edge("designer", "implementer")
        g.add_edge("implementer", "reviewer")
        g.add_edge("reviewer", "implementer")
        g.add_edge("reviewer", GraphNode.END)
        compiled = g.compile(
            max_iterations=15,
            cycle_detection="off",
            scheduler=SchedulerKind.PARALLEL,
            default_trigger=NodeTrigger.ON_ALL_PREDS,
        )

        coord = _make_coordinator(compiled)
        ctx = _make_ctx(
            coord,
            user_input="Write a one-line Python function that reverses a string.",
        )
        ctx.scheduler_kind = SchedulerKind.PARALLEL
        runtime.bind_graph_context(ctx)

        try:
            engine = GraphEngine(compiled)
            result = await engine.run_async(ctx, mode=BootstrapMode.FRESH)
            assert result is not None
            assert result.result is not None
            assert len(result.result) > 0
        finally:
            await runtime.close()


# ── Part 7: E2E — reviewer→implementer loop with memory persistence ─────


class _MockDesignerNode(Node[DefaultGraphState]):
    """Delivers a fixed spec to implementer — no LLM."""

    async def execute(
        self, ctx: GraphContext[DefaultGraphState], integrated_input: IntegratedInput
    ) -> None:
        self.deliver(
            GraphPayload(
                content="Design spec: Write a Python function reverse_string(s) "
                "that reverses a string using slicing s[::-1]."
            ),
            "implementer",
            ctx,
        )


class _MockReviewerNode(Node[DefaultGraphState]):
    """Rejects first, approves second — deterministic loop control."""

    def __init__(self) -> None:
        super().__init__()
        self.call_count = 0

    async def execute(
        self, ctx: GraphContext[DefaultGraphState], integrated_input: IntegratedInput
    ) -> None:
        self.call_count += 1
        if self.call_count == 1:
            self.deliver(
                GraphPayload(
                    content="NEEDS_REVISION: rename function to 'rev' and add docstring"
                ),
                "implementer",
                ctx,
            )
        else:
            self.deliver(
                GraphPayload(content="APPROVED"),
                GraphNode.END,
                ctx,
            )


class _TrackingContextManager:
    """Wraps a ContextManager to intercept load/save and capture history length."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.load_calls: list[str] = []
        self.save_calls: list[str] = []
        self.history_lengths_on_load: list[int] = []

    async def load(self, session_id: str, **kwargs: Any) -> Any:
        self.load_calls.append(session_id)
        state = await self._inner.load(session_id, **kwargs)
        try:
            msgs = await state.history.to_list()
            self.history_lengths_on_load.append(len(msgs))
        except Exception:
            self.history_lengths_on_load.append(-1)
        return state

    async def save(self, session_id: str, **kwargs: Any) -> None:
        self.save_calls.append(session_id)
        await self._inner.save(session_id, **kwargs)

    async def flush(self, session_id: str) -> None:
        await self._inner.flush(session_id)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _InstrumentedBotAgentNode(BotAgentNode):
    """BotAgentNode that records session_id on each execute call."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.session_ids_per_turn: list[str] = []

    async def execute(
        self, ctx: GraphContext[DefaultGraphState], integrated_input: IntegratedInput
    ) -> None:
        session = await self._ensure_session(ctx)
        self.session_ids_per_turn.append(session.session_id)
        await super().execute(ctx, integrated_input)


@pytest.mark.integration
class TestE2EReviewLoopWithMemory:
    """E2E: reviewer→implementer loop with CACHED session + memory persistence.

    - designer: mock (fixed spec, no LLM)
    - implementer: real BotAgentNode with LLM (CACHED) — key test subject
    - reviewer: mock (rejects first, approves second)

    Verifies:
    1. implementer runs exactly twice (rejected then approved)
    2. Both turns use the SAME session_id (CACHED strategy)
    3. Session registered only once in registry
    4. Memory persists: second turn's loaded history is longer than first
    5. Reviewer called exactly twice
    6. Graph result contains APPROVED
    """

    async def test_loop_with_memory_persistence(
        self, tmp_path: Path, e2e_model_config: Any
    ) -> None:
        from bot.graph.agent_node import SessionStrategy

        from modex_agent.core.capabilities import Modality, ModelCapabilities, ModelInfo
        from modex_agent.core.constants import ExecutionStrategyKind
        from modex_agent.core.llm_struct import (
            LLMTimeoutPolicy,
            RuntimeSafetyPolicy,
            TurnTimeoutPolicy,
        )
        from modex_agent.core.scope import MemoryAgentRole
        from modex_agent.ioc.configs.llm import LLMConfig
        from modex_agent.ioc.factories.descriptors import build_session_only_memory
        from modex_agent.ioc.factories.llm import create_llm_provider
        from modex_agent.multi_agent.address import AgentAddress
        from modex_agent.multi_agent.comm_kind import AgentCommKind
        from modex_agent.multi_agent.descriptor import (
            AgentDescriptor,
            AgentLLMConfig,
        )

        mc = e2e_model_config
        provider = create_llm_provider(
            LLMConfig(
                model=mc.model,
                api_key=mc.api_key,
                base_url=mc.base_url,
                temperature=mc.temperature,
                max_output_tokens=mc.max_output_tokens,
                reasoning_effort=mc.reasoning_effort,
            )
        )
        caps = ModelInfo(
            model_name=mc.model,
            capabilities=ModelCapabilities(modalities=frozenset({Modality.TEXT})),
        )
        safety = RuntimeSafetyPolicy(
            llm=LLMTimeoutPolicy(
                request_timeout_seconds=45.0,
                stream_idle_timeout_seconds=90.0,
                framework_max_retries=1,
                retry_backoff_seconds=(2.0, 8.0),
            ),
            turn=TurnTimeoutPolicy(
                dispatch_timeout_seconds=120.0,
                hook_timeout_seconds=10.0,
            ),
        )

        broker = InMemoryMessageBroker()
        await broker.start()
        factory = DefaultAgentFactory(default_llm_provider=provider)

        agents_dir = _FIXTURES / "agents"
        prompt = (agents_dir / "implementer.md").read_text(encoding="utf-8")

        raw_memory_ctx = build_session_only_memory(
            cfg=None,
            workspace=tmp_path / "memory" / "implementer",
            agent_id="implementer",
            agent_role=MemoryAgentRole.SUBAGENT,
            system_prompt=prompt,
            pruned_manager=None,
        )
        tracking_ctx = _TrackingContextManager(raw_memory_ctx)

        descriptor = AgentDescriptor(
            address=AgentAddress(name="implementer"),
            role_description="Implementer agent",
            llm_config=AgentLLMConfig(
                model=mc.model,
                temperature=mc.temperature,
                max_output_tokens=mc.max_output_tokens,
                reasoning_effort=mc.reasoning_effort,
                model_info=caps,
            ),
            system_prompt_template=prompt,
            max_iterations=10,
            execution_strategy=ExecutionStrategyKind.REACT,
            context_strategy="persistent",
            comm_kind=AgentCommKind.NORMAL,
            safety_policy=safety,
        )
        instance = await factory.create_agent(
            descriptor,
            broker=broker,
            tool_manager=None,
            skill_resolver=None,
            context_manager=tracking_ctx,  # type: ignore[arg-type]
            hooks=[],
        )

        register_count = [0]

        async def _counting_register(session: SessionInfo) -> None:
            register_count[0] += 1

        session_registry = InMemorySessionRegistry(on_register=_counting_register)

        runtime = _ReviewPoolRuntime(broker, factory, session_registry)
        await runtime.register({"implementer": instance})

        class _FakeWS:
            def __init__(self) -> None:
                self.pools = {"review": runtime}
                self.pool_data: dict[str, Any] = {}
                self.ctx = SimpleNamespace(
                    paths=WorkspacePaths(root=tmp_path / ".modex")
                )

        class _FakeResolver:
            def resolve_workspace(self) -> Any:
                return _FakeWS()

        resolver: Any = _FakeResolver()
        implementer = _InstrumentedBotAgentNode(
            "implementer",
            "review",
            resolver,
            session_strategy=SessionStrategy.CACHED,
        )

        g: Graph[DefaultGraphState] = Graph()
        g.add_node("designer", _MockDesignerNode())
        g.add_node("implementer", implementer)
        g.add_node("reviewer", _MockReviewerNode())
        g.add_edge(GraphNode.START, "designer")
        g.add_edge("designer", "implementer")
        g.add_edge("implementer", "reviewer")
        g.add_edge("reviewer", "implementer")
        g.add_edge("reviewer", GraphNode.END)
        compiled = g.compile(
            max_iterations=15,
            cycle_detection="off",
            scheduler=SchedulerKind.PARALLEL,
            default_trigger=NodeTrigger.ON_ALL_PREDS,
        )

        coord = _make_coordinator(compiled)
        ctx = _make_ctx(
            coord,
            user_input="Write a Python function that reverses a string.",
        )
        ctx.scheduler_kind = SchedulerKind.PARALLEL
        runtime.bind_graph_context(ctx)

        try:
            engine = GraphEngine(compiled)
            result = await engine.run_async(ctx, mode=BootstrapMode.FRESH)

            # 1. Graph completed
            assert result is not None
            assert result.result is not None
            assert len(result.result) > 0

            # 2. Implementer ran exactly twice
            assert len(implementer.session_ids_per_turn) == 2

            # 3. Both turns used the SAME session_id (CACHED)
            sid_1, sid_2 = implementer.session_ids_per_turn
            assert sid_1 == sid_2, (
                f"CACHED strategy failed: first turn sid={sid_1!r}, "
                f"second turn sid={sid_2!r}"
            )

            # 4. Session registered only once (CACHED → single registration)
            assert register_count[0] == 1, (
                f"Expected 1 registration (CACHED), got {register_count[0]}"
            )

            # 5. Memory persisted: second load has more messages than first
            impl_loads = [
                i for i, sid in enumerate(tracking_ctx.load_calls) if sid == sid_1
            ]
            assert len(impl_loads) >= 2, (
                f"Expected ≥2 load() calls for implementer session, "
                f"got {len(impl_loads)}"
            )
            first_load_len = tracking_ctx.history_lengths_on_load[impl_loads[0]]
            second_load_len = tracking_ctx.history_lengths_on_load[impl_loads[1]]
            assert second_load_len > first_load_len, (
                f"Memory did not persist: first load had {first_load_len} messages, "
                f"second load had {second_load_len} messages (expected growth)"
            )

            # 6. Reviewer called exactly twice (reject then approve)
            reviewer_node = compiled.nodes["reviewer"]
            assert isinstance(reviewer_node, _MockReviewerNode)
            assert reviewer_node.call_count == 2

            # 7. Graph result contains APPROVED
            approved = any(
                "APPROVED" in (p.content or "") for p in (result.result or [])
            )
            assert approved, "Graph result does not contain APPROVED"
        finally:
            await runtime.close()
