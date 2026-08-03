"""Tests for ParallelScheduler graph_instance_id management + checkpoint recovery.

Covers P1C.3 (graph_instance_id as persistence key), P1C.4 (CheckpointStore
load_latest connection), P1C.5 (recovery flow), P1C.8 (DispatchStore recovery
query path).

Test groups:

- Fresh start: no checkpoint → identical to pre-recovery behavior.
  - ``test_fresh_start_no_graph_instance_id`` — ctx.graph_instance_id=None
    → Snowflake ID generated, run_id is numeric string.
  - ``test_fresh_start_with_graph_instance_id`` — ctx.graph_instance_id=12345
    → uses 12345 as the persistence key.
  - ``test_no_recovery_when_checkpoint_store_empty`` — no checkpoint for
    the graph_instance_id → fresh start.

- Checkpoint content: after a run, the checkpoint has the ticket-10-class-1
  fields.
  - ``test_checkpoint_contains_new_fields`` — graph_instance_id,
    activated_sources, instance_seq, iteration_count.

- Recovery: load_latest → rebuild state → skip completed → re-dispatch.
  - ``test_recovery_from_checkpoint`` — full A→B→END run, recover with
    same graph_instance_id → state restored, no re-execution.
  - ``test_recovery_skips_completed_instances`` — completed_instances in
    checkpoint → recovery does NOT re-execute them.
  - ``test_recovery_restores_main_state`` — checkpoint.main_state has
    modified state → recovery restores it.
  - ``test_recovery_redispatches_pending`` — checkpoint with
    pending_on_all_preds → _recheck_pending re-dispatches the target.

- DispatchStore recovery path (P1C.8):
  - ``test_query_dispatches_by_target_after_run`` — the helper returns
    dispatches for the current run.
  - ``test_query_dispatches_by_target_before_run`` — returns [] before
    run_async sets run_id.

Backward compatibility is critical: all existing tests in test_scheduler.py
and test_parallel_scheduler.py MUST pass unchanged.
"""

from __future__ import annotations

import asyncio

from helpers import CounterState, make_runtime

from modex_graph import (
    CheckpointData,
    Graph,
    GraphContext,
    GraphEngine,
    GraphNode,
    InstanceRecord,
    IntegratedInput,
    MemoryCheckpointStore,
    Node,
    NodeInstanceStatus,
    NodeResult,
    NodeTrigger,
    ParallelScheduler,
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
) -> GraphContext[CounterState]:
    """Build a GraphContext configured for ParallelScheduler."""
    return GraphContext(
        state=state if state is not None else CounterState(),
        runtime=make_runtime(),
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


async def flush_checkpoints(scheduler: ParallelScheduler[CounterState]) -> None:
    """Wait for all pending checkpoint save tasks to complete."""
    tasks = list(scheduler._checkpoint_tasks)
    if tasks:
        await asyncio.gather(*tasks)


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
        """Two runs with graph_instance_id=None → different Snowflake IDs."""
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
        """Fresh start with no graph_instance_id produces correct result."""
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


# ── No recovery when checkpoint store empty ───────────────────────────────


class TestNoRecoveryWhenEmpty:
    """No checkpoint for the graph_instance_id → fresh start."""

    async def test_empty_store_fresh_start(self) -> None:
        g = make_linear_graph()
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)
        checkpoint_store = MemoryCheckpointStore()

        ctx = make_parallel_ctx(CounterState(count=0), graph_instance_id=77777)
        scheduler = ParallelScheduler(compiled, checkpoint_store=checkpoint_store)
        await scheduler.run_async(ctx)

        # Fresh start — graph executed normally.
        assert ctx.state.count == 3
        assert scheduler._iteration_count == 2

    async def test_no_checkpoint_for_this_id_fresh_start(self) -> None:
        """Checkpoint exists for a different graph_instance_id → fresh start."""
        g = make_linear_graph()
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)
        checkpoint_store = MemoryCheckpointStore()

        # Save a checkpoint under a different graph_instance_id.
        other_checkpoint = CheckpointData(
            main_state=CounterState(count=99).checkpoint(),
            pending_on_all_preds={},
            graph_instance_id=11111,
        )
        await checkpoint_store.save(other_checkpoint, "11111")

        # Run with a different graph_instance_id — no checkpoint found.
        ctx = make_parallel_ctx(CounterState(count=0), graph_instance_id=22222)
        scheduler = ParallelScheduler(compiled, checkpoint_store=checkpoint_store)
        await scheduler.run_async(ctx)

        # Fresh start — count is 3 (1+2), not 99.
        assert ctx.state.count == 3


# ── Checkpoint contains new fields ────────────────────────────────────────


class TestCheckpointContainsNewFields:
    """After a run, the checkpoint has the ticket-10-class-1 fields."""

    async def test_checkpoint_has_graph_instance_id(self) -> None:
        g = make_linear_graph()
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)
        checkpoint_store = MemoryCheckpointStore()

        ctx = make_parallel_ctx(CounterState(count=0), graph_instance_id=55555)
        scheduler = ParallelScheduler(compiled, checkpoint_store=checkpoint_store)
        await scheduler.run_async(ctx)
        await flush_checkpoints(scheduler)

        checkpoint = await checkpoint_store.load_latest("55555")
        assert checkpoint is not None
        assert checkpoint.graph_instance_id == 55555

    async def test_checkpoint_has_iteration_count(self) -> None:
        g = make_linear_graph()
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)
        checkpoint_store = MemoryCheckpointStore()

        ctx = make_parallel_ctx(CounterState(count=0), graph_instance_id=44444)
        scheduler = ParallelScheduler(compiled, checkpoint_store=checkpoint_store)
        await scheduler.run_async(ctx)
        await flush_checkpoints(scheduler)

        checkpoint = await checkpoint_store.load_latest("44444")
        assert checkpoint is not None
        # A→B→END = 2 iterations.
        assert checkpoint.iteration_count == 2

    async def test_checkpoint_has_instance_seq(self) -> None:
        g = make_linear_graph()
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)
        checkpoint_store = MemoryCheckpointStore()

        ctx = make_parallel_ctx(CounterState(count=0), graph_instance_id=33333)
        scheduler = ParallelScheduler(compiled, checkpoint_store=checkpoint_store)
        await scheduler.run_async(ctx)
        await flush_checkpoints(scheduler)

        checkpoint = await checkpoint_store.load_latest("33333")
        assert checkpoint is not None
        # Two instances created (a#0, b#1), so instance_seq=2.
        assert checkpoint.instance_seq == 2

    async def test_checkpoint_has_completed_instances(self) -> None:
        g = make_linear_graph()
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)
        checkpoint_store = MemoryCheckpointStore()

        ctx = make_parallel_ctx(CounterState(count=0), graph_instance_id=22222)
        scheduler = ParallelScheduler(compiled, checkpoint_store=checkpoint_store)
        await scheduler.run_async(ctx)
        await flush_checkpoints(scheduler)

        checkpoint = await checkpoint_store.load_latest("22222")
        assert checkpoint is not None
        assert len(checkpoint.completed_instances) == 2
        ids = {inst.instance_id for inst in checkpoint.completed_instances}
        assert ids == {"a#0", "b#1"}

    async def test_checkpoint_has_activated_sources(self) -> None:
        """activated_sources is empty for a linear ON_RECEIVE graph."""
        g = make_linear_graph()
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)
        checkpoint_store = MemoryCheckpointStore()

        ctx = make_parallel_ctx(CounterState(count=0), graph_instance_id=11111)
        scheduler = ParallelScheduler(compiled, checkpoint_store=checkpoint_store)
        await scheduler.run_async(ctx)
        await flush_checkpoints(scheduler)

        checkpoint = await checkpoint_store.load_latest("11111")
        assert checkpoint is not None
        # Linear graph uses default ON_RECEIVE — no activated_sources.
        assert checkpoint.activated_sources == {}

    async def test_checkpoint_activated_sources_is_dict_field(self) -> None:
        """activated_sources is present as a dict field in the checkpoint.

        In a completed run, activated_sources is empty — all ON_ALL_PREDS
        targets fired and ``_try_fire_on_all_preds`` cleared their tracking.
        The populated case is tested in ``TestRecoveryRedispatchesPending``
        via a manually-constructed checkpoint with activated_sources set.
        """
        g: Graph[CounterState] = Graph()
        g.add_node("a", DispatchAddNode(amount=1, target="b"))
        g.add_node("b", DispatchAddNode(amount=10, target=GraphNode.END))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", "b")
        g.add_edge("b", GraphNode.END)
        compiled = g.compile(
            scheduler=SchedulerKind.PARALLEL,
            default_trigger=NodeTrigger.ON_ALL_PREDS,
        )
        checkpoint_store = MemoryCheckpointStore()

        ctx = make_parallel_ctx(CounterState(count=0), graph_instance_id=66666)
        scheduler = ParallelScheduler(compiled, checkpoint_store=checkpoint_store)
        await scheduler.run_async(ctx)
        await flush_checkpoints(scheduler)

        checkpoint = await checkpoint_store.load_latest("66666")
        assert checkpoint is not None
        assert isinstance(checkpoint.activated_sources, dict)

    async def test_checkpoint_activated_sources_populated_in_intermediate(self) -> None:
        """Intermediate checkpoint (before a pending target fires) has
        activated_sources populated.

        Graph: a dispatches to x and y (both ON_ALL_PREDS). x→y edge means
        y is blocked while x is active. The checkpoint saved after ``a``
        completes (but before ``x`` fires ``y``) has activated_sources for
        the still-pending target.
        """

        class FanOutNode(Node[CounterState]):
            def __init__(self, amount: int, t1: str, t2: str) -> None:
                self.amount = amount
                self.t1 = t1
                self.t2 = t2

            def execute(self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput) -> NodeResult:
                ctx.state.count += self.amount
                self.deliver(None, self.t1, ctx)
                self.deliver(None, self.t2, ctx)
                return NodeResult()

        g: Graph[CounterState] = Graph()
        g.add_node("a", FanOutNode(amount=1, t1="x", t2="y"))
        g.add_node("x", DispatchAddNode(amount=10, target="y"))
        g.add_node("y", DispatchAddNode(amount=100, target=GraphNode.END))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", "x")
        g.add_edge("a", "y")
        g.add_edge("a", GraphNode.END)
        g.add_edge("x", "y")
        g.add_edge("x", GraphNode.END)
        g.add_edge("y", GraphNode.END)
        compiled = g.compile(
            scheduler=SchedulerKind.PARALLEL,
            default_trigger=NodeTrigger.ON_ALL_PREDS,
        )
        checkpoint_store = MemoryCheckpointStore()

        ctx = make_parallel_ctx(CounterState(count=0), graph_instance_id=66667)
        scheduler = ParallelScheduler(compiled, checkpoint_store=checkpoint_store)
        await scheduler.run_async(ctx)
        await flush_checkpoints(scheduler)

        # The first checkpoint (saved after "a" completed) should have
        # activated_sources for "y" (still pending — blocked by "x").
        all_checkpoints = checkpoint_store._checkpoints.get("66667", [])
        assert len(all_checkpoints) >= 1
        first = all_checkpoints[0]
        assert "y" in first.activated_sources
        assert "a" in first.activated_sources["y"]


# ── Recovery from checkpoint ──────────────────────────────────────────────


class TestRecoveryFromCheckpoint:
    """Save checkpoint → new scheduler with same store → recover."""

    async def test_recovery_restores_state(self) -> None:
        """Full run → recover → state is restored from checkpoint."""
        g = make_linear_graph()
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)
        checkpoint_store = MemoryCheckpointStore()

        # First run: A(+1) → B(+2) → END. count=3.
        ctx1 = make_parallel_ctx(CounterState(count=0), graph_instance_id=88888)
        scheduler1 = ParallelScheduler(compiled, checkpoint_store=checkpoint_store)
        await scheduler1.run_async(ctx1)
        await flush_checkpoints(scheduler1)
        assert ctx1.state.count == 3

        # Second run (recovery): same graph_instance_id.
        ctx2 = make_parallel_ctx(CounterState(count=0), graph_instance_id=88888)
        scheduler2 = ParallelScheduler(compiled, checkpoint_store=checkpoint_store)
        await scheduler2.run_async(ctx2)

        # State restored from checkpoint — count=3, not re-executed from 0.
        assert ctx2.state.count == 3

    async def test_recovery_does_not_re_execute(self) -> None:
        """Recovery does not re-execute completed instances."""
        g = make_linear_graph()
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)
        checkpoint_store = MemoryCheckpointStore()

        ctx1 = make_parallel_ctx(CounterState(count=0), graph_instance_id=88889)
        scheduler1 = ParallelScheduler(compiled, checkpoint_store=checkpoint_store)
        await scheduler1.run_async(ctx1)
        await flush_checkpoints(scheduler1)

        ctx2 = make_parallel_ctx(CounterState(count=0), graph_instance_id=88889)
        scheduler2 = ParallelScheduler(compiled, checkpoint_store=checkpoint_store)
        await scheduler2.run_async(ctx2)

        # iteration_count restored from checkpoint (2), not reset to 0.
        assert scheduler2._iteration_count == 2
        # No new instances created — completed instances not re-added.
        assert len(scheduler2._instances) == 0
        # No instances in _active or _ready.
        assert len(scheduler2._active) == 0
        assert len(scheduler2._ready) == 0

    async def test_recovery_skips_completed_instances(self) -> None:
        """Checkpoint with completed_instances → recovery does NOT re-execute."""
        g = make_linear_graph()
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)
        checkpoint_store = MemoryCheckpointStore()

        ctx1 = make_parallel_ctx(CounterState(count=0), graph_instance_id=88890)
        scheduler1 = ParallelScheduler(compiled, checkpoint_store=checkpoint_store)
        await scheduler1.run_async(ctx1)
        await flush_checkpoints(scheduler1)

        # Verify the checkpoint has both a#0 and b#1 completed.
        checkpoint = await checkpoint_store.load_latest("88890")
        assert checkpoint is not None
        completed_ids = {inst.instance_id for inst in checkpoint.completed_instances}
        assert completed_ids == {"a#0", "b#1"}

        # Recover.
        ctx2 = make_parallel_ctx(CounterState(count=0), graph_instance_id=88890)
        scheduler2 = ParallelScheduler(compiled, checkpoint_store=checkpoint_store)
        await scheduler2.run_async(ctx2)

        # No instances in _instances — completed ones were not re-added.
        assert len(scheduler2._instances) == 0
        # Count is the checkpointed value (3), not re-incremented.
        assert ctx2.state.count == 3

    async def test_recovery_restores_main_state(self) -> None:
        """checkpoint.main_state has modified state → recovery restores it."""
        g: Graph[CounterState] = Graph()
        g.add_node("a", DispatchAddNode(amount=42, target=GraphNode.END))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)
        checkpoint_store = MemoryCheckpointStore()

        # First run: count goes from 0 to 42.
        ctx1 = make_parallel_ctx(CounterState(count=0), graph_instance_id=88891)
        scheduler1 = ParallelScheduler(compiled, checkpoint_store=checkpoint_store)
        await scheduler1.run_async(ctx1)
        await flush_checkpoints(scheduler1)
        assert ctx1.state.count == 42

        # Recover with a fresh state (count=0) — should be overwritten.
        ctx2 = make_parallel_ctx(CounterState(count=0), graph_instance_id=88891)
        scheduler2 = ParallelScheduler(compiled, checkpoint_store=checkpoint_store)
        await scheduler2.run_async(ctx2)

        # State restored from checkpoint — count=42, not 0.
        assert ctx2.state.count == 42

    async def test_recovery_restores_instance_seq(self) -> None:
        """Recovery restores _instance_seq so new instances get correct IDs."""
        g = make_linear_graph()
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)
        checkpoint_store = MemoryCheckpointStore()

        ctx1 = make_parallel_ctx(CounterState(count=0), graph_instance_id=88892)
        scheduler1 = ParallelScheduler(compiled, checkpoint_store=checkpoint_store)
        await scheduler1.run_async(ctx1)
        await flush_checkpoints(scheduler1)
        assert scheduler1._instance_seq == 2

        ctx2 = make_parallel_ctx(CounterState(count=0), graph_instance_id=88892)
        scheduler2 = ParallelScheduler(compiled, checkpoint_store=checkpoint_store)
        await scheduler2.run_async(ctx2)

        assert scheduler2._instance_seq == 2

    async def test_recovery_with_sqlite_checkpoint_store(self) -> None:
        """Recovery works with SqliteCheckpointStore too."""
        from modex_graph import SqliteCheckpointStore

        g = make_linear_graph()
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)
        checkpoint_store = SqliteCheckpointStore(":memory:")

        try:
            ctx1 = make_parallel_ctx(CounterState(count=0), graph_instance_id=88893)
            scheduler1 = ParallelScheduler(compiled, checkpoint_store=checkpoint_store)
            await scheduler1.run_async(ctx1)
            await flush_checkpoints(scheduler1)
            assert ctx1.state.count == 3

            ctx2 = make_parallel_ctx(CounterState(count=0), graph_instance_id=88893)
            scheduler2 = ParallelScheduler(compiled, checkpoint_store=checkpoint_store)
            await scheduler2.run_async(ctx2)

            assert ctx2.state.count == 3
        finally:
            checkpoint_store.close()


# ── Recovery: re-dispatch pending ON_ALL_PREDS ────────────────────────────


class TestRecoveryRedispatchesPending:
    """Checkpoint with pending_on_all_preds → _recheck_pending re-dispatches."""

    async def test_pending_target_is_redispatched(self) -> None:
        """A checkpoint with a pending ON_ALL_PREDS target → recovery
        re-dispatches it (creates instance, marks READY, executes)."""
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
        checkpoint_store = MemoryCheckpointStore()

        # Build a checkpoint simulating: "a" completed (count=1), "b" has
        # a pending dispatch from "a" but hasn't fired yet.
        state_snapshot = CounterState(count=1).checkpoint()
        checkpoint = CheckpointData(
            main_state=state_snapshot,
            pending_on_all_preds={"b": {"a": [None]}},
            completed_instances=[
                InstanceRecord(
                    instance_id="a#0",
                    node_name="a",
                    fork_version=0,
                    status=NodeInstanceStatus.COMPLETED,
                ),
            ],
            dispatch_events=[],
            graph_instance_id=77778,
            activated_sources={"b": ["a"]},
            instance_seq=1,
            iteration_count=1,
        )
        await checkpoint_store.save(checkpoint, "77778")

        # Recover.
        ctx = make_parallel_ctx(CounterState(count=0), graph_instance_id=77778)
        scheduler = ParallelScheduler(compiled, checkpoint_store=checkpoint_store)
        await scheduler.run_async(ctx)

        # State restored to count=1, then "b" re-dispatched and executed
        # (count += 10 → 11).
        assert ctx.state.count == 11
        # "b" was executed — iteration_count went from 1 (restored) to 2.
        assert scheduler._iteration_count == 2
        # "b" instance was created and completed.
        assert len(scheduler._instances) == 1
        b_instance = list(scheduler._instances.values())[0]
        assert b_instance.node_name == "b"
        assert b_instance.status == NodeInstanceStatus.COMPLETED

    async def test_pending_not_fired_when_reachability_blocked(self) -> None:
        """A pending target with a self-referencing reachability block
        does not fire on recovery if another pending target can reach it.

        We construct two pending targets where one can reach the other,
        so the reachability BFS prevents the second from firing.
        """
        g: Graph[CounterState] = Graph()
        g.add_node("a", DispatchAddNode(amount=1, target=GraphNode.END))
        g.add_node("x", DispatchAddNode(amount=100, target="y"))
        g.add_node("y", DispatchAddNode(amount=200, target=GraphNode.END))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", "x")
        g.add_edge("a", "y")
        g.add_edge("x", "y")
        g.add_edge("x", GraphNode.END)
        g.add_edge("y", GraphNode.END)
        compiled = g.compile(
            scheduler=SchedulerKind.PARALLEL,
            default_trigger=NodeTrigger.ON_ALL_PREDS,
        )
        checkpoint_store = MemoryCheckpointStore()

        # Checkpoint: "a" completed, both "x" and "y" have pending
        # dispatches from "a". "x" can reach "y" (x→y edge), so "y" is
        # blocked until "x" fires. But "x" has no reachability block,
        # so "x" fires first, then "y" fires after "x" completes.
        state_snapshot = CounterState(count=1).checkpoint()
        checkpoint = CheckpointData(
            main_state=state_snapshot,
            pending_on_all_preds={
                "x": {"a": [None]},
                "y": {"a": [None]},
            },
            completed_instances=[
                InstanceRecord(
                    instance_id="a#0",
                    node_name="a",
                    fork_version=0,
                    status=NodeInstanceStatus.COMPLETED,
                ),
            ],
            dispatch_events=[],
            graph_instance_id=77779,
            activated_sources={"x": ["a"], "y": ["a"]},
            instance_seq=1,
            iteration_count=1,
        )
        await checkpoint_store.save(checkpoint, "77779")

        ctx = make_parallel_ctx(CounterState(count=0), graph_instance_id=77779)
        scheduler = ParallelScheduler(compiled, checkpoint_store=checkpoint_store)
        await scheduler.run_async(ctx)

        # "x" fired (+100), then "y" fired (+200). count = 1 + 100 + 200 = 301.
        assert ctx.state.count == 301
        assert scheduler._iteration_count == 3


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

        # A→B→END: a#0 dispatches to "b", b#1 dispatches to END.
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


# ── Backward compatibility ────────────────────────────────────────────────


class TestBackwardCompat:
    """Fresh start (no checkpoint, no graph_instance_id) is identical to before."""

    async def test_default_checkpoint_store_is_memory(self) -> None:
        g: Graph[CounterState] = Graph()
        g.add_node("a", DispatchAddNode(amount=1, target=GraphNode.END))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        scheduler = ParallelScheduler(compiled)
        assert isinstance(scheduler._checkpoint_store, MemoryCheckpointStore)

    async def test_run_without_checkpoint_store_arg(self) -> None:
        """Scheduler works without explicit checkpoint_store (default Memory)."""
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
        """run_id is a non-empty string (backward compat with uuid hex tests)."""
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
