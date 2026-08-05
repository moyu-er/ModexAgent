"""Tests for ParallelScheduler graph_instance_id management + coordinator recovery.

Covers coordinator-driven recovery via ``load_for_recovery``.

Test groups:

- Fresh start: Null coordinator (no prior invocations) → identical to
  pre-recovery behavior.
  - ``test_fresh_start_no_graph_instance_id`` — ctx.graph_instance_id=None
  → Snowflake ID generated.
  - ``test_fresh_start_with_graph_instance_id`` — ctx.graph_instance_id=12345
    → uses 12345 as the persistence key.
  - ``test_null_coordinator_fresh_start`` — Null coordinator → fresh start.

- Recovery via mocked coordinator: ``load_for_recovery`` returns a
  pre-built ``RecoveryContext`` → scheduler rebuilds state.
  - ``test_recovery_restores_state`` — rebuilt_main_state → ctx.state restored.
  - ``test_recovery_restores_counters`` — iteration_count derived from
    COMPLETED invocations; instance_seq reset to 0.
  - ``test_recovery_skips_completed_nodes`` — COMPLETED nodes not re-executed.
  - ``test_recovery_redispatches_crashed`` — CRASHED → re-dispatched.
  - ``test_recovery_skips_canceled`` — CANCELED → not re-dispatched.
  - ``test_recovery_redispatches_pending_on_all_preds`` — PENDING delivers
    → _recheck_pending fires the target.
  - ``test_recovery_f5_pending_delivers_for_completed`` — COMPLETED
    node with PENDING delivers → deliver scan creates instance.

"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

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
    NodeInvocationRecord,
    NodeTrigger,
    ParallelScheduler,
    RecoveryContext,
    SchedulerKind,
)

# ── Test helpers ──────────────────────────────────────────────────────────


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
    """A→B→END linear graph under ParallelScheduler."""
    g: Graph[CounterState] = Graph()
    g.add_node("a", DispatchAddNode(amount=1, target="b"))
    g.add_node("b", DispatchAddNode(amount=2, target=GraphNode.END))
    g.add_edge(GraphNode.START, "a")
    g.add_edge("a", "b")
    g.add_edge("b", GraphNode.END)
    return g


def _make_recovery_context(
    *,
    graph_instance_id: int = 88888,
    node_states: dict[str, NodeInvocationRecord | None] | None = None,
    rebuilt_main_state: dict[str, Any] | None = None,
) -> RecoveryContext:
    """Build a RecoveryContext for testing."""
    return RecoveryContext(
        metadata=GraphMetadata(
            graph_instance_id=graph_instance_id,
            spec_id=0,
            parent_instance_id=None,
            parent_node=None,
            status=GraphInstanceStatus.RUNNING,
        ),
        node_states=node_states or {},
        rebuilt_main_state=rebuilt_main_state or {},
    )


def _make_invocation_record(
    node_name: str,
    *,
    status: InvocationStatus,
    invocation_id: int = 1,
    version: int = 0,
    suspended: bool = False,
) -> NodeInvocationRecord:
    return NodeInvocationRecord(
        graph_instance_id=0,
        node_name=node_name,
        invocation_id=invocation_id,
        version=version,
        parent_version=None,
        status=status,
        state_json={},
        suspended=suspended,
        created_at=0,
        updated_at=0,
    )


# ── Fresh start: with graph_instance_id ───────────────────────────────────


class TestFreshStartWithGraphInstanceId:
    """ctx.graph_instance_id set → uses it as the persistence key."""

    async def test_uses_provided_id(self) -> None:
        g: Graph[CounterState] = Graph()
        g.add_node("a", DispatchAddNode(amount=5, target=GraphNode.END))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = make_parallel_ctx(CounterState(count=0), graph_instance_id=12345)
        scheduler = ParallelScheduler(compiled)
        await scheduler.run_async(ctx)

        assert ctx.state.count == 5

    async def test_executes_normally(self) -> None:
        g = make_linear_graph()
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = make_parallel_ctx(CounterState(count=0), graph_instance_id=99999)
        await GraphEngine(compiled).run_async(ctx)
        assert ctx.state.count == 3


# ── Null coordinator → fresh start ────────────────────────────────────────


class TestNullCoordinatorFreshStart:
    """Null coordinator (no prior invocations) → fresh start."""

    async def test_null_coordinator_fresh_start(self) -> None:
        g = make_linear_graph()
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = make_parallel_ctx(CounterState(count=0), graph_instance_id=77777)
        scheduler = ParallelScheduler(compiled)
        await scheduler.run_async(ctx)

        assert ctx.state.count == 3
        assert scheduler._iteration_count == 2


# ── Recovery via mocked coordinator ───────────────────────────────────────


class TestRecoveryFromCoordinator:
    """load_for_recovery returns prior state → scheduler rebuilds."""

    async def test_recovery_restores_state(self) -> None:
        """rebuilt_main_state → ctx.state restored from it."""
        g = make_linear_graph()
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        recovery = _make_recovery_context(
            graph_instance_id=88888,
            node_states={
                "a": _make_invocation_record(
                    "a", status=InvocationStatus.COMPLETED, invocation_id=100
                ),
                "b": _make_invocation_record(
                    "b", status=InvocationStatus.COMPLETED, invocation_id=101
                ),
            },
            rebuilt_main_state={"count": 3, "name": ""},
        )

        coord = make_coordinator(("a", "b"))
        with patch.object(coord, "load_for_recovery", return_value=recovery):
            ctx = make_parallel_ctx(
                CounterState(count=0),
                graph_instance_id=88888,
                coordinator=coord,
            )
            scheduler = ParallelScheduler(compiled)
            await scheduler.run_async(ctx)

        assert ctx.state.count == 3

    async def test_recovery_restores_counters(self) -> None:
        """iteration_count derived from COMPLETED invocations; instance_seq reset to 0."""
        g = make_linear_graph()
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        coord = make_coordinator(("a", "b"))
        store = coord.node_state_store
        inv_a = store.begin_invocation("a")
        store.complete_invocation(inv_a, {"count": 3})
        inv_b = store.begin_invocation("b")
        store.complete_invocation(inv_b, {"count": 3})

        recovery = _make_recovery_context(
            graph_instance_id=88889,
            node_states={
                "a": _make_invocation_record(
                    "a", status=InvocationStatus.COMPLETED, invocation_id=100
                ),
                "b": _make_invocation_record(
                    "b", status=InvocationStatus.COMPLETED, invocation_id=101
                ),
            },
            rebuilt_main_state={"count": 3, "name": ""},
        )

        with patch.object(coord, "load_for_recovery", return_value=recovery):
            ctx = make_parallel_ctx(
                CounterState(count=0),
                graph_instance_id=88889,
                coordinator=coord,
            )
            scheduler = ParallelScheduler(compiled)
            await scheduler.run_async(ctx)

        assert scheduler._iteration_count == 2
        assert scheduler._instance_seq == 0

    async def test_recovery_skips_completed_nodes(self) -> None:
        """COMPLETED nodes are NOT re-executed."""
        g = make_linear_graph()
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        recovery = _make_recovery_context(
            graph_instance_id=88890,
            node_states={
                "a": _make_invocation_record(
                    "a", status=InvocationStatus.COMPLETED, invocation_id=100
                ),
                "b": _make_invocation_record(
                    "b", status=InvocationStatus.COMPLETED, invocation_id=101
                ),
            },
            rebuilt_main_state={"count": 3, "name": ""},
        )

        coord = make_coordinator(("a", "b"))
        with patch.object(coord, "load_for_recovery", return_value=recovery):
            ctx = make_parallel_ctx(
                CounterState(count=0),
                graph_instance_id=88890,
                coordinator=coord,
            )
            scheduler = ParallelScheduler(compiled)
            await scheduler.run_async(ctx)

        assert len(scheduler._instances) == 0
        assert len(scheduler._active) == 0
        assert len(scheduler._ready) == 0
        assert ctx.state.count == 3

    async def test_recovery_redispatches_crashed(self) -> None:
        """CRASHED node → re-dispatched."""
        g: Graph[CounterState] = Graph()
        g.add_node("a", DispatchAddNode(amount=10, target=GraphNode.END))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        recovery = _make_recovery_context(
            graph_instance_id=88892,
            node_states={
                "a": _make_invocation_record(
                    "a", status=InvocationStatus.CRASHED, invocation_id=100
                ),
            },
            rebuilt_main_state={"count": 0, "name": ""},
        )

        coord = make_coordinator(("a",))
        with patch.object(coord, "load_for_recovery", return_value=recovery):
            ctx = make_parallel_ctx(
                CounterState(count=0),
                graph_instance_id=88892,
                coordinator=coord,
            )
            scheduler = ParallelScheduler(compiled)
            await scheduler.run_async(ctx)

        assert ctx.state.count == 10
        assert scheduler._iteration_count == 1

    async def test_recovery_skips_canceled(self) -> None:
        """CANCELED node → NOT re-dispatched."""
        g: Graph[CounterState] = Graph()
        g.add_node("a", DispatchAddNode(amount=10, target=GraphNode.END))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        recovery = _make_recovery_context(
            graph_instance_id=88893,
            node_states={
                "a": _make_invocation_record(
                    "a", status=InvocationStatus.CANCELED, invocation_id=100
                ),
            },
            rebuilt_main_state={"count": 0, "name": ""},
        )

        coord = make_coordinator(("a",))
        with patch.object(coord, "load_for_recovery", return_value=recovery):
            ctx = make_parallel_ctx(
                CounterState(count=0),
                graph_instance_id=88893,
                coordinator=coord,
            )
            scheduler = ParallelScheduler(compiled)
            await scheduler.run_async(ctx)

        assert ctx.state.count == 0
        assert scheduler._iteration_count == 0
        assert len(scheduler._instances) == 0

    async def test_recovery_redispatches_pending_on_all_preds(self) -> None:
        """PENDING delivers → _recheck_pending fires ON_ALL_PREDS target."""
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

        coord = make_coordinator(("a", "b"))
        store = coord.node_state_store
        inv_a = store.begin_invocation("a")
        store.complete_invocation(inv_a, {"count": 1})
        coord.route_deliver("b", None, "a", 100)

        recovery = _make_recovery_context(
            graph_instance_id=77778,
            node_states={
                "a": _make_invocation_record(
                    "a", status=InvocationStatus.COMPLETED, invocation_id=100
                ),
                "b": None,
            },
            rebuilt_main_state={"count": 1, "name": ""},
        )

        with patch.object(coord, "load_for_recovery", return_value=recovery):
            ctx = make_parallel_ctx(
                CounterState(count=0),
                graph_instance_id=77778,
                coordinator=coord,
            )
            scheduler = ParallelScheduler(compiled)
            await scheduler.run_async(ctx)

        assert ctx.state.count == 11
        assert scheduler._iteration_count == 2
        assert len(scheduler._instances) == 1
        b_instance = list(scheduler._instances.values())[0]
        assert b_instance.node_name == "b"
        assert b_instance.status == NodeInstanceStatus.COMPLETED

    async def test_recovery_f5_pending_delivers_for_completed(self) -> None:
        """COMPLETED node with PENDING delivers → deliver scan creates instance."""
        from modex_graph import (
            InMemoryDeliverStoreFactory,
            InMemoryNodeStateStore,
            NullGraphInstanceStore,
        )

        g: Graph[CounterState] = Graph()
        g.add_node("a", DispatchAddNode(amount=5, target=GraphNode.END))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        from helpers import _AutoRegisterCoordinator

        coord = _AutoRegisterCoordinator(
            graph_instance_id=88895,
            instance_store=NullGraphInstanceStore(),
            node_state_store=InMemoryNodeStateStore(88895),
            default_deliver_store_factory=InMemoryDeliverStoreFactory(),
        )
        coord.register_node("a")

        store = coord.node_state_store
        inv_a = store.begin_invocation("a")
        store.complete_invocation(inv_a, {"count": 5})

        deliver_store = coord.get_deliver_store("a")
        assert deliver_store is not None
        deliver_store.accumulate(
            graph_instance_id=88895,
            target_node="a",
            source_node="external",
            source_invocation_id=0,
            content={"data": "pending"},
        )

        recovery = _make_recovery_context(
            graph_instance_id=88895,
            node_states={
                "a": _make_invocation_record(
                    "a", status=InvocationStatus.COMPLETED, invocation_id=100
                ),
            },
            rebuilt_main_state={"count": 5, "name": ""},
        )

        with patch.object(coord, "load_for_recovery", return_value=recovery):
            ctx = make_parallel_ctx(
                CounterState(count=0),
                graph_instance_id=88895,
                coordinator=coord,
            )
            scheduler = ParallelScheduler(compiled)
            await scheduler.run_async(ctx)

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
        for node_name in ("a", "b", "c"):
            coordinator.register_node(node_name)

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

        states_before = coordinator.load_for_recovery().node_states
        assert states_before["a"] is not None
        assert states_before["a"].status == InvocationStatus.CRASHED
        assert states_before["b"] is not None
        assert states_before["b"].status == InvocationStatus.RUNNING
        assert states_before["b"].suspended is True
        assert states_before["c"] is not None
        assert states_before["c"].status == InvocationStatus.COMPLETED

        for node_name in ("a", "b", "c"):
            coordinator.route_deliver(
                node_name,
                f"pending-{node_name}",
                "external",
                0,
            )

        scheduler = ParallelScheduler(compiled)
        await scheduler.run_async(setup_context())

        assert node_a.execute_count == 2
        assert node_a.inputs[-1] == ["pending-a"]
        assert node_b.execute_count == 2
        assert node_b.inputs[-1] == [
            {"resume_target": None, "count": 0, "name": "", "messages": []},
            "pending-b",
        ]
        assert node_c.execute_count == 2
        assert node_c.inputs[-1] == ["pending-c"]

        store_a = coordinator.get_deliver_store("a")
        store_b = coordinator.get_deliver_store("b")
        store_c = coordinator.get_deliver_store("c")
        assert store_a is not None
        assert store_b is not None
        assert store_c is not None
        assert store_a.query_consumable(graph_instance_id, "a") == []
        assert store_b.query_consumable(graph_instance_id, "b") == []
        assert store_c.query_consumable(graph_instance_id, "c") == []

        versions = coordinator.node_state_store.query_all(set(InvocationStatus))
        assert sum(record.node_name == "a" for record in versions) == 2
        assert sum(record.node_name == "b" for record in versions) == 2
        assert sum(record.node_name == "c" for record in versions) == 2


# ── Basic execution ───────────────────────────────────────────────────────


class TestBasicExecution:
    """Scheduler works without explicit stores (defaults)."""

    async def test_run_without_explicit_stores(self) -> None:
        g = make_linear_graph()
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = make_parallel_ctx(CounterState(count=0))
        scheduler = ParallelScheduler(compiled)
        await scheduler.run_async(ctx)
        assert ctx.state.count == 3


# ── LinearScheduler recovery ──────────────────────────────────────────────


class CountSnapshotNode(Node[CounterState]):
    """Appends str(count) to messages, then delivers to default downstream."""

    async def execute(
        self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput
    ) -> None:
        ctx.state.messages.append(str(ctx.state.count))
        self.deliver(None, None, ctx)
        return None


class TestLinearSchedulerRecovery:
    """LinearScheduler restores state from recovery context, then runs from entry."""

    async def test_restores_state_from_recovery_context(self) -> None:
        g: Graph[CounterState] = Graph()
        g.add_node("a", CountSnapshotNode())
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.LINEAR)

        recovery = _make_recovery_context(
            node_states={
                "a": _make_invocation_record(
                    "a", status=InvocationStatus.COMPLETED, invocation_id=100
                ),
            },
            rebuilt_main_state={"count": 42},
        )

        coord = make_coordinator(("a",))
        with patch.object(coord, "load_for_recovery", return_value=recovery):
            ctx = GraphContext(
                state=CounterState(count=0),
                runtime=make_runtime(),
                coordinator=coord,
                scheduler_kind=SchedulerKind.LINEAR,
            )
            scheduler = LinearScheduler(compiled)
            await scheduler.run_async(ctx)

        assert ctx.state.count == 42
        assert ctx.state.messages == ["42"]

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
        await scheduler.run_async(ctx)

        assert ctx.state.count == 0
        assert ctx.state.messages == ["0"]
