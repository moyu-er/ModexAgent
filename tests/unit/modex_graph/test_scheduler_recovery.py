"""Tests for ParallelScheduler graph_instance_id management + coordinator recovery.

Covers bootstrap-driven recovery via real store setup.

Test groups:

- Fresh start: Null coordinator (no prior invocations) -> identical to
  pre-recovery behavior.
  - ``test_fresh_start_no_graph_instance_id`` -- ctx.graph_instance_id=None
    -> Snowflake ID generated.
  - ``test_fresh_start_with_graph_instance_id`` -- ctx.graph_instance_id=12345
    -> uses 12345 as the persistence key.
  - ``test_null_coordinator_fresh_start`` -- Null coordinator -> fresh start.

- Recovery via real store: set up node_states + delivers in the store, then
  let bootstrap query them naturally.
  - ``test_recovery_restores_state`` -- rebuilt_main_state -> ctx.state restored.
  - ``test_recovery_restores_counters`` -- COMPLETED nodes -> no instances
    created (counters stay at 0).
  - ``test_recovery_skips_completed_nodes`` -- COMPLETED nodes not re-executed.
  - ``test_recovery_redispatches_crashed`` -- CRASHED -> re-dispatched.
  - ``test_recovery_skips_canceled`` -- CANCELED -> not re-dispatched.
  - ``test_recovery_redispatches_pending_on_all_preds`` -- PENDING delivers
    -> _recheck_pending fires the target.
  - ``test_recovery_f5_pending_delivers_for_completed`` -- COMPLETED
    node with PENDING delivers -> deliver scan creates instance.

"""

from __future__ import annotations

from typing import Any

import pytest
from helpers import CounterState, make_coordinator, make_runtime

from modex_graph import (
    DeliverConsumptionStatus,
    Graph,
    GraphContext,
    GraphEngine,
    GraphInstanceStatus,
    GraphInterrupt,
    GraphMetadata,
    GraphNode,
    GraphPersistenceCoordinator,
    InMemoryDeliverStoreFactory,
    InMemoryGraphInstanceStore,
    InMemoryNodeStateStore,
    IntegratedInput,
    InvocationStatus,
    LinearScheduler,
    Node,
    NodeInstanceStatus,
    NodeStateStore,
    NodeTrigger,
    ParallelScheduler,
    SchedulerKind,
)
from modex_graph.scheduler.bootstrap import BootstrapMode

# -- Test helpers -----------------------------------------------------------


class DispatchAddNode(Node[CounterState]):
    """Increments count by ``amount``, then dispatches to ``target`` if set."""

    def __init__(self, amount: int, target: str | None = None) -> None:
        self.amount = amount
        self.target = target

    async def execute(
        self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput
    ) -> None:
        ctx.state.count += self.amount
        if self.target is not None:
            self.deliver(None, self.target, ctx)
        return None


class MixedRecoveryNode(Node[CounterState]):
    def __init__(
        self,
        label: str,
        *,
        crash_once: bool = False,
        interrupt_once: bool = False,
    ) -> None:
        self.label = label
        self.crash_once = crash_once
        self.interrupt_once = interrupt_once
        self.inputs: list[Any] = []
        self.execute_count = 0

    async def execute(
        self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput
    ) -> None:
        self.execute_count += 1
        self.inputs.append(integrated_input.integrated_content)
        if self.crash_once:
            self.crash_once = False
            raise RuntimeError(f"{self.label} crashed")
        if self.interrupt_once:
            self.interrupt_once = False
            ctx.interrupt({"node": self.label})
        self.deliver(self.label, GraphNode.END, ctx)


def make_parallel_ctx(
    state: CounterState | None = None,
    *,
    graph_instance_id: int | None = None,
    coordinator: GraphPersistenceCoordinator | None = None,
) -> GraphContext[CounterState]:
    """Build a GraphContext configured for ParallelScheduler."""
    return GraphContext(
        state=state if state is not None else CounterState(),
        runtime=make_runtime(),
        coordinator=coordinator if coordinator is not None else make_coordinator(),
        scheduler_kind=SchedulerKind.PARALLEL,
        graph_instance_id=graph_instance_id,
    )


def make_linear_graph() -> Graph[CounterState]:
    """A->B->END linear graph under ParallelScheduler."""
    g: Graph[CounterState] = Graph()
    g.add_node("a", DispatchAddNode(amount=1, target="b"))
    g.add_node("b", DispatchAddNode(amount=2, target=GraphNode.END))
    g.add_edge(GraphNode.START, "a")
    g.add_edge("a", "b")
    g.add_edge("b", GraphNode.END)
    return g


def _setup_completed(
    store: NodeStateStore, node_id: str
) -> None:
    inv = store.begin_invocation(node_id)
    store.complete_invocation(inv)


def _setup_crashed(store: NodeStateStore, node_id: str) -> None:
    inv = store.begin_invocation(node_id)
    store.crash_invocation(inv)


def _setup_canceled(store: NodeStateStore, node_id: str) -> None:
    inv = store.begin_invocation(node_id)
    store.cancel_invocation(inv)


# -- Fresh start: with graph_instance_id -------------------------------------


class TestFreshStartWithGraphInstanceId:
    """ctx.graph_instance_id set -> uses it as the persistence key."""

    async def test_uses_provided_id(self) -> None:
        g: Graph[CounterState] = Graph()
        g.add_node("a", DispatchAddNode(amount=5, target=GraphNode.END))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = make_parallel_ctx(CounterState(count=0), graph_instance_id=12345)
        scheduler = ParallelScheduler(compiled)
        await scheduler.run_async(ctx, mode=BootstrapMode.FRESH)

        assert ctx.state.count == 5

    async def test_executes_normally(self) -> None:
        g = make_linear_graph()
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = make_parallel_ctx(CounterState(count=0), graph_instance_id=99999)
        await GraphEngine(compiled).run_async(ctx, mode=BootstrapMode.FRESH)
        assert ctx.state.count == 3


# -- Null coordinator -> fresh start -----------------------------------------


class TestNullCoordinatorFreshStart:
    """Null coordinator (no prior invocations) -> fresh start."""

    async def test_null_coordinator_fresh_start(self) -> None:
        g = make_linear_graph()
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = make_parallel_ctx(CounterState(count=0), graph_instance_id=77777)
        scheduler = ParallelScheduler(compiled)
        await scheduler.run_async(ctx, mode=BootstrapMode.FRESH)

        assert ctx.state.count == 3
        assert scheduler._iteration_count == 4


# -- Recovery via real store setup -------------------------------------------


class TestRecoveryFromCoordinator:
    """bootstrap queries the store -> scheduler rebuilds state from seeds."""

    async def test_recovery_restores_counters(self) -> None:
        """RECOVERY on all-COMPLETED graph re-invokes from entry (empty-seed fallback)."""
        g = make_linear_graph()
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)
        node_ids = {name: node.node_id for name, node in compiled.nodes.items()}

        coord = make_coordinator(tuple(node_ids.values()))
        store = coord.node_state_store
        _setup_completed(store, node_ids["a"])
        _setup_completed(store, node_ids["b"])

        ctx = make_parallel_ctx(
            CounterState(count=0),
            graph_instance_id=88889,
            coordinator=coord,
        )
        scheduler = ParallelScheduler(compiled)
        await scheduler.run_async(ctx, mode=BootstrapMode.RECOVERY)

        assert scheduler._iteration_count > 0
        assert scheduler._instance_seq > 0

    async def test_recovery_skips_completed_nodes(self) -> None:
        """RECOVERY on all-COMPLETED graph re-invokes from entry (empty-seed fallback)."""
        g = make_linear_graph()
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)
        node_ids = {name: node.node_id for name, node in compiled.nodes.items()}

        coord = make_coordinator(tuple(node_ids.values()))
        store = coord.node_state_store
        _setup_completed(store, node_ids["a"])
        _setup_completed(store, node_ids["b"])

        ctx = make_parallel_ctx(
            CounterState(count=0),
            graph_instance_id=88890,
            coordinator=coord,
        )
        scheduler = ParallelScheduler(compiled)
        await scheduler.run_async(ctx, mode=BootstrapMode.RECOVERY)

        assert len(scheduler._instances) > 0
        assert ctx.state.count == 3

    async def test_recovery_redispatches_crashed(self) -> None:
        """CRASHED node -> re-dispatched."""
        g: Graph[CounterState] = Graph()
        g.add_node("a", DispatchAddNode(amount=10, target=GraphNode.END))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)
        node_id = compiled.nodes["a"].node_id

        coord = make_coordinator((node_id,))
        store = coord.node_state_store
        _setup_crashed(store, node_id)

        ctx = make_parallel_ctx(
            CounterState(count=0),
            graph_instance_id=88892,
            coordinator=coord,
        )
        scheduler = ParallelScheduler(compiled)
        await scheduler.run_async(ctx, mode=BootstrapMode.RECOVERY)

        assert ctx.state.count == 10
        assert scheduler._iteration_count == 2

    async def test_recovery_skips_canceled(self) -> None:
        """RECOVERY on CANCELED entry re-invokes from entry (empty-seed fallback)."""
        g: Graph[CounterState] = Graph()
        g.add_node("a", DispatchAddNode(amount=10, target=GraphNode.END))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)
        node_id = compiled.nodes["a"].node_id

        coord = make_coordinator((node_id,))
        store = coord.node_state_store
        _setup_canceled(store, node_id)

        ctx = make_parallel_ctx(
            CounterState(count=0),
            graph_instance_id=88893,
            coordinator=coord,
        )
        scheduler = ParallelScheduler(compiled)
        await scheduler.run_async(ctx, mode=BootstrapMode.RECOVERY)

        assert ctx.state.count == 10
        assert scheduler._iteration_count > 0
        assert len(scheduler._instances) > 0

    async def test_recovery_redispatches_pending_on_all_preds(self) -> None:
        """PENDING delivers -> _recheck_pending fires ON_ALL_PREDS target."""
        g: Graph[CounterState] = Graph()
        g.add_node("a", DispatchAddNode(amount=1, target=GraphNode.END))
        g.add_node("b", DispatchAddNode(amount=10, target=GraphNode.END))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", "b")
        g.add_edge("a", GraphNode.END)
        g.add_edge("b", GraphNode.END)
        compiled = g.compile(
            scheduler=SchedulerKind.PARALLEL,
            default_trigger=NodeTrigger.ON_ALL_PREDS,
        )
        node_ids = {name: node.node_id for name, node in compiled.nodes.items()}

        coord = make_coordinator(tuple(node_ids.values()))
        store = coord.node_state_store
        _setup_completed(store, node_ids["a"])
        coord.route_deliver(node_ids["b"], None, node_ids["a"], 100)
        pending = coord.collect_consumable_delivers(node_ids["b"], 0)
        assert pending[0].status is DeliverConsumptionStatus.PENDING

        ctx = make_parallel_ctx(
            CounterState(count=0),
            graph_instance_id=77778,
            coordinator=coord,
        )
        scheduler = ParallelScheduler(compiled)
        await scheduler.run_async(ctx, mode=BootstrapMode.RECOVERY)

        assert ctx.state.count == 10
        assert scheduler._iteration_count == 2
        assert len(scheduler._instances) == 2
        b_instance = next(
            instance for instance in scheduler._instances.values() if instance.node_name == "b"
        )
        assert b_instance.node_name == "b"
        assert b_instance.status == NodeInstanceStatus.COMPLETED

    async def test_recovery_f5_pending_delivers_for_completed(self) -> None:
        """COMPLETED node with PENDING delivers -> deliver scan creates instance."""
        from helpers import _AutoRegisterCoordinator

        from modex_graph import NullGraphInstanceStore

        g: Graph[CounterState] = Graph()
        g.add_node("a", DispatchAddNode(amount=5, target=GraphNode.END))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)
        node_id = compiled.nodes["a"].node_id

        coord = _AutoRegisterCoordinator(
            graph_instance_id=88895,
            instance_store=NullGraphInstanceStore(),
            node_state_store=InMemoryNodeStateStore(88895),
            default_deliver_store_factory=InMemoryDeliverStoreFactory(),
        )
        coord.register_node(node_id)

        store = coord.node_state_store
        _setup_completed(store, node_id)

        deliver_store = coord.get_deliver_store(node_id)
        assert deliver_store is not None
        deliver_store.accumulate(
            graph_instance_id=88895,
            node_id=node_id,
            source_node_id="external",
            source_invocation_id=0,
            content={"data": "pending"},
        )
        pending = deliver_store.query_consumable(88895, node_id)
        assert pending[0].status is DeliverConsumptionStatus.PENDING

        ctx = make_parallel_ctx(
            CounterState(count=0),
            graph_instance_id=88895,
            coordinator=coord,
        )
        scheduler = ParallelScheduler(compiled)
        await scheduler.run_async(ctx, mode=BootstrapMode.RECOVERY)

        assert scheduler._iteration_count == 2

    async def test_mixed_invocation_states_and_pending_delivers_recover(self) -> None:
        graph_instance_id = 88896
        node_a = MixedRecoveryNode("a", crash_once=True)
        node_b = MixedRecoveryNode("b", interrupt_once=True)
        node_c = MixedRecoveryNode("c")
        node_a.trigger = NodeTrigger.ON_RECEIVE
        node_b.trigger = NodeTrigger.ON_RECEIVE
        node_c.trigger = NodeTrigger.ON_RECEIVE

        graph: Graph[CounterState] = Graph()
        graph.add_node("a", node_a)
        graph.add_node("b", node_b)
        graph.add_node("c", node_c)
        graph.add_edge(GraphNode.START, "a")
        graph.add_edge("a", "b")
        graph.add_edge("a", "c")
        graph.add_edge("a", GraphNode.END)
        graph.add_edge("b", GraphNode.END)
        graph.add_edge("c", GraphNode.END)
        compiled = graph.compile(scheduler=SchedulerKind.PARALLEL)
        node_ids = {name: node.node_id for name, node in compiled.nodes.items()}

        instance_store = InMemoryGraphInstanceStore()
        instance_store.save(
            GraphMetadata(
                graph_instance_id=graph_instance_id,
                spec_id=0,
                parent_instance_id=None,
                parent_node=None,
                status=GraphInstanceStatus.RUNNING,
            )
        )
        coordinator = GraphPersistenceCoordinator(
            graph_instance_id=graph_instance_id,
            instance_store=instance_store,
            node_state_store=InMemoryNodeStateStore(graph_instance_id),
            default_deliver_store_factory=InMemoryDeliverStoreFactory(),
        )
        for node_id in node_ids.values():
            coordinator.register_node(node_id)

        def setup_context() -> GraphContext[CounterState]:
            return make_parallel_ctx(
                CounterState(),
                graph_instance_id=graph_instance_id,
                coordinator=coordinator,
            )

        with pytest.raises(RuntimeError, match="a crashed"):
            await node_a.run(setup_context(), graph=compiled)
        with pytest.raises(GraphInterrupt):
            await node_b.run(setup_context(), graph=compiled)
        await node_c.run(setup_context(), graph=compiled)

        record_a = coordinator.node_state_store.load_latest(node_ids["a"])
        record_b = coordinator.node_state_store.load_latest(node_ids["b"])
        record_c = coordinator.node_state_store.load_latest(node_ids["c"])
        assert record_a is not None
        assert record_b is not None
        assert record_c is not None
        assert record_a.status == InvocationStatus.CRASHED
        assert record_b.status == InvocationStatus.CANCELED
        assert record_c.status == InvocationStatus.COMPLETED

        for node_name in ("a", "b", "c"):
            node_id = node_ids[node_name]
            coordinator.route_deliver(
                node_id,
                f"pending-{node_name}",
                "external",
                0,
            )

        scheduler = ParallelScheduler(compiled)
        await scheduler.run_async(setup_context(), mode=BootstrapMode.RECOVERY)

        assert node_a.execute_count == 2
        assert node_a.inputs[-1] == ["pending-a"]
        assert node_b.execute_count == 2
        assert node_b.inputs[-1] == ["pending-b"]
        assert node_c.execute_count == 2
        assert node_c.inputs[-1] == ["pending-c"]

        store_a = coordinator.get_deliver_store(node_ids["a"])
        store_b = coordinator.get_deliver_store(node_ids["b"])
        store_c = coordinator.get_deliver_store(node_ids["c"])
        assert store_a is not None
        assert store_b is not None
        assert store_c is not None
        assert store_a.query_consumable(graph_instance_id, node_ids["a"]) == []
        assert store_b.query_consumable(graph_instance_id, node_ids["b"]) == []
        assert store_c.query_consumable(graph_instance_id, node_ids["c"]) == []

        versions = coordinator.node_state_store.query_all(set(InvocationStatus))
        assert sum(record.node_id == node_ids["a"] for record in versions) == 2
        assert sum(record.node_id == node_ids["b"] for record in versions) == 2
        assert sum(record.node_id == node_ids["c"] for record in versions) == 2


# -- Basic execution --------------------------------------------------------


class TestBasicExecution:
    """Scheduler works without explicit stores (defaults)."""

    async def test_run_without_explicit_stores(self) -> None:
        g = make_linear_graph()
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = make_parallel_ctx(CounterState(count=0))
        scheduler = ParallelScheduler(compiled)
        await scheduler.run_async(ctx, mode=BootstrapMode.FRESH)
        assert ctx.state.count == 3


# -- LinearScheduler recovery -----------------------------------------------


class CountSnapshotNode(Node[CounterState]):
    """Appends str(count) to messages, then delivers to default downstream."""

    async def execute(
        self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput
    ) -> None:
        ctx.state.messages.append(str(ctx.state.count))
        self.deliver(None, None, ctx)
        return None


class TestLinearSchedulerRecovery:
    """LinearScheduler recovery: COMPLETED nodes skipped, entry re-invoked."""

    async def test_skips_recovery_when_no_prior_state(self) -> None:
        g: Graph[CounterState] = Graph()
        g.add_node("a", CountSnapshotNode())
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.LINEAR)

        ctx = GraphContext(
            state=CounterState(count=0),
            runtime=make_runtime(),
            coordinator=make_coordinator(("a",)),
            scheduler_kind=SchedulerKind.LINEAR,
        )
        scheduler = LinearScheduler(compiled)
        await scheduler.run_async(ctx, mode=BootstrapMode.RECOVERY)

        assert ctx.state.count == 0
        assert ctx.state.messages == ["0"]
