"""Tests for ParallelScheduler graph_instance_id management + coordinator recovery.

Covers coordinator-driven recovery via ``load_for_recovery``.

Test groups:

- Fresh start: Null coordinator (no prior invocations) → identical to
  pre-recovery behavior.
  - ``test_fresh_start_no_graph_instance_id`` — ctx.graph_instance_id=None
    → Snowflake ID generated, run_id is numeric string.
  - ``test_fresh_start_with_graph_instance_id`` — ctx.graph_instance_id=12345
    → uses 12345 as the persistence key.
  - ``test_null_coordinator_fresh_start`` — Null coordinator → fresh start.

- Recovery via mocked coordinator: ``load_for_recovery`` returns a
  pre-built ``RecoveryContext`` → scheduler rebuilds state.
  - ``test_recovery_restores_state`` — rebuilt_main_state → ctx.state restored.
  - ``test_recovery_restores_counters`` — metadata counters restored.
  - ``test_recovery_skips_completed_nodes`` — COMPLETED nodes not re-executed.
  - ``test_recovery_redispatches_superseded`` — SUPERSEDED with no
    successor → re-dispatched.
  - ``test_recovery_redispatches_crashed`` — CRASHED → re-dispatched.
  - ``test_recovery_skips_canceled`` — CANCELED → not re-dispatched.
  - ``test_recovery_redispatches_pending_on_all_preds`` — pending_dispatches
    from metadata → _recheck_pending fires the target.
  - ``test_recovery_f5_pending_delivers_for_completed`` — COMPLETED
    node with PENDING delivers in deliver_store → re-dispatched.

- DispatchStore recovery path:
  - ``test_query_dispatches_by_target_after_run`` — the helper returns
    dispatches for the current run.
  - ``test_query_dispatches_by_target_before_run`` — returns [] before
    run_async sets run_id.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from helpers import CounterState, make_coordinator, make_runtime

from modex_graph import (
    Graph,
    GraphContext,
    GraphEngine,
    GraphInstanceStatus,
    GraphMetadata,
    GraphNode,
    GraphPersistenceCoordinator,
    IntegratedInput,
    InvocationStatus,
    Node,
    NodeInstanceStatus,
    NodeInvocationRecord,
    NodeResult,
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

    def execute(self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput) -> NodeResult:
        ctx.state.count += self.amount
        if self.target is not None:
            self.deliver(None, self.target, ctx)
        return NodeResult()


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
    iteration_count: int = 0,
    instance_seq: int = 0,
    activated_sources: dict[str, list[str]] | None = None,
    pending_dispatches: dict[str, dict[str, list[dict[str, Any] | None]]] | None = None,
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
            instance_seq=instance_seq,
            iteration_count=iteration_count,
            activated_sources=activated_sources or {},
            pending_dispatches=pending_dispatches or {},
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


# ── Fresh start: no graph_instance_id ─────────────────────────────────────


class TestFreshStartNoGraphInstanceId:
    """ctx.graph_instance_id=None → Snowflake ID generated, fresh start."""

    async def test_generates_snowflake_id(self) -> None:
        g: Graph[CounterState] = Graph()
        g.add_node("a", DispatchAddNode(amount=5, target=GraphNode.END))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = make_parallel_ctx(CounterState(count=0))
        assert ctx.graph_instance_id is None

        scheduler = ParallelScheduler(compiled)
        await scheduler.run_async(ctx)

        assert scheduler._graph_instance_id > 0
        assert scheduler._run_id is not None
        assert scheduler._run_id == str(scheduler._graph_instance_id)

    async def test_run_id_is_numeric_string(self) -> None:
        g: Graph[CounterState] = Graph()
        g.add_node("a", DispatchAddNode(amount=1, target=GraphNode.END))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = make_parallel_ctx(CounterState(count=0))
        scheduler = ParallelScheduler(compiled)
        await scheduler.run_async(ctx)

        assert scheduler._run_id is not None
        assert scheduler._run_id.isdigit()
        assert int(scheduler._run_id) > 0

    async def test_run_id_is_none_before_run(self) -> None:
        g: Graph[CounterState] = Graph()
        g.add_node("a", DispatchAddNode(amount=1, target=GraphNode.END))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        scheduler = ParallelScheduler(compiled)
        assert scheduler._run_id is None
        assert scheduler._graph_instance_id == 0

    async def test_two_runs_produce_different_ids(self) -> None:
        g: Graph[CounterState] = Graph()
        g.add_node("a", DispatchAddNode(amount=1, target=GraphNode.END))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        scheduler = ParallelScheduler(compiled)

        ctx1 = make_parallel_ctx(CounterState(count=0))
        await scheduler.run_async(ctx1)
        id1 = scheduler._graph_instance_id

        ctx2 = make_parallel_ctx(CounterState(count=0))
        await scheduler.run_async(ctx2)
        id2 = scheduler._graph_instance_id

        assert id1 != id2
        assert id1 > 0
        assert id2 > 0

    async def test_fresh_start_executes_normally(self) -> None:
        g = make_linear_graph()
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = make_parallel_ctx(CounterState(count=0))
        await GraphEngine(compiled).run_async(ctx)
        assert ctx.state.count == 3


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

        assert scheduler._graph_instance_id == 12345
        assert scheduler._run_id == "12345"

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
            iteration_count=2,
            instance_seq=2,
            node_states={
                "a": _make_invocation_record("a", status=InvocationStatus.COMPLETED, invocation_id=100),
                "b": _make_invocation_record("b", status=InvocationStatus.COMPLETED, invocation_id=101),
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
        """metadata.iteration_count + instance_seq restored."""
        g = make_linear_graph()
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        recovery = _make_recovery_context(
            graph_instance_id=88889,
            iteration_count=2,
            instance_seq=2,
            node_states={
                "a": _make_invocation_record("a", status=InvocationStatus.COMPLETED, invocation_id=100),
                "b": _make_invocation_record("b", status=InvocationStatus.COMPLETED, invocation_id=101),
            },
            rebuilt_main_state={"count": 3, "name": ""},
        )

        coord = make_coordinator(("a", "b"))
        with patch.object(coord, "load_for_recovery", return_value=recovery):
            ctx = make_parallel_ctx(
                CounterState(count=0),
                graph_instance_id=88889,
                coordinator=coord,
            )
            scheduler = ParallelScheduler(compiled)
            await scheduler.run_async(ctx)

        assert scheduler._iteration_count == 2
        assert scheduler._instance_seq == 2

    async def test_recovery_skips_completed_nodes(self) -> None:
        """COMPLETED nodes are NOT re-executed."""
        g = make_linear_graph()
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        recovery = _make_recovery_context(
            graph_instance_id=88890,
            iteration_count=2,
            instance_seq=2,
            node_states={
                "a": _make_invocation_record("a", status=InvocationStatus.COMPLETED, invocation_id=100),
                "b": _make_invocation_record("b", status=InvocationStatus.COMPLETED, invocation_id=101),
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

    async def test_recovery_redispatches_superseded_f1(self) -> None:
        """F1: SUPERSEDED with no successor → re-dispatched."""
        g: Graph[CounterState] = Graph()
        g.add_node("a", DispatchAddNode(amount=42, target=GraphNode.END))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        recovery = _make_recovery_context(
            graph_instance_id=88891,
            iteration_count=0,
            instance_seq=1,
            node_states={
                "a": _make_invocation_record(
                    "a", status=InvocationStatus.SUPERSEDED, invocation_id=100, suspended=True
                ),
            },
            rebuilt_main_state={"count": 0, "name": ""},
        )

        coord = make_coordinator(("a",))
        with patch.object(coord, "load_for_recovery", return_value=recovery):
            ctx = make_parallel_ctx(
                CounterState(count=0),
                graph_instance_id=88891,
                coordinator=coord,
            )
            scheduler = ParallelScheduler(compiled)
            await scheduler.run_async(ctx)

        assert ctx.state.count == 42
        assert scheduler._iteration_count == 1

    async def test_recovery_redispatches_crashed(self) -> None:
        """CRASHED node → re-dispatched."""
        g: Graph[CounterState] = Graph()
        g.add_node("a", DispatchAddNode(amount=10, target=GraphNode.END))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        recovery = _make_recovery_context(
            graph_instance_id=88892,
            iteration_count=0,
            instance_seq=1,
            node_states={
                "a": _make_invocation_record("a", status=InvocationStatus.CRASHED, invocation_id=100),
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
            iteration_count=0,
            instance_seq=1,
            node_states={
                "a": _make_invocation_record("a", status=InvocationStatus.CANCELED, invocation_id=100),
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
        """pending_dispatches from metadata → _recheck_pending fires target."""
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

        recovery = _make_recovery_context(
            graph_instance_id=77778,
            iteration_count=1,
            instance_seq=1,
            activated_sources={"b": ["a"]},
            pending_dispatches={"b": {"a": [None]}},
            node_states={
                "a": _make_invocation_record("a", status=InvocationStatus.COMPLETED, invocation_id=100),
                "b": None,
            },
            rebuilt_main_state={"count": 1, "name": ""},
        )

        coord = make_coordinator(("a", "b"))
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
        """F5: COMPLETED node with PENDING delivers in deliver_store → re-dispatched."""
        from modex_graph import (
            InMemoryDeliverStoreFactory,
            SimpleNodeStateFactory,
        )

        g: Graph[CounterState] = Graph()
        g.add_node("a", DispatchAddNode(amount=5, target=GraphNode.END))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        # Build a coordinator with InMemoryDeliverStore so delivers persist.
        from helpers import _AutoRegisterCoordinator

        from modex_graph import NullGraphMetadataStore

        coord = _AutoRegisterCoordinator(
            graph_instance_id=88895,
            graph_metadata_store=NullGraphMetadataStore(),
            default_node_state_factory=SimpleNodeStateFactory(),
            default_deliver_store_factory=InMemoryDeliverStoreFactory(),
        )
        coord.register_node("a")

        # Manually add a PENDING deliver to "a"'s deliver_store.
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
            iteration_count=1,
            instance_seq=1,
            node_states={
                "a": _make_invocation_record("a", status=InvocationStatus.COMPLETED, invocation_id=100),
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

        # "a" was re-dispatched because it had a PENDING deliver.
        assert scheduler._iteration_count == 2


# ── DispatchStore recovery path (P1C.8) ───────────────────────────────────


class TestQueryDispatchesByTarget:
    """query_dispatches_by_target helper (P1C.8)."""

    async def test_returns_empty_before_run(self) -> None:
        g: Graph[CounterState] = Graph()
        g.add_node("a", DispatchAddNode(amount=1, target=GraphNode.END))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        scheduler = ParallelScheduler(compiled)
        assert scheduler.query_dispatches_by_target("a") == []

    async def test_returns_dispatches_after_run(self) -> None:
        g = make_linear_graph()
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = make_parallel_ctx(CounterState(count=0), graph_instance_id=88894)
        scheduler = ParallelScheduler(compiled)
        await scheduler.run_async(ctx)

        to_b = scheduler.query_dispatches_by_target("b")
        assert len(to_b) == 1
        assert to_b[0].source_instance == "a#0"
        assert to_b[0].target == "b"

        to_end = scheduler.query_dispatches_by_target(GraphNode.END)
        assert len(to_end) == 1
        assert to_end[0].source_instance == "b#1"

    async def test_returns_empty_for_unknown_target(self) -> None:
        g = make_linear_graph()
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = make_parallel_ctx(CounterState(count=0), graph_instance_id=88895)
        scheduler = ParallelScheduler(compiled)
        await scheduler.run_async(ctx)

        assert scheduler.query_dispatches_by_target("nonexistent") == []


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

    async def test_dispatch_log_still_works(self) -> None:
        """_dispatch_log property still returns events after run."""
        g = make_linear_graph()
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = make_parallel_ctx(CounterState(count=0))
        engine = GraphEngine(compiled)
        await engine.run_async(ctx)

        scheduler = engine._scheduler
        assert isinstance(scheduler, ParallelScheduler)
        assert len(scheduler._dispatch_log) == 2
        assert scheduler._dispatch_log[0].source_instance == "a#0"
        assert scheduler._dispatch_log[0].target == "b"

    async def test_run_id_is_nonempty_string(self) -> None:
        """run_id is a non-empty string."""
        g: Graph[CounterState] = Graph()
        g.add_node("a", DispatchAddNode(amount=1, target=GraphNode.END))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        scheduler = ParallelScheduler(compiled)
        assert scheduler._run_id is None
        await scheduler.run_async(make_parallel_ctx(CounterState()))
        assert scheduler._run_id is not None
        assert isinstance(scheduler._run_id, str)
        assert len(scheduler._run_id) > 0
