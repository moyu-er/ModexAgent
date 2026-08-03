"""Fork state isolation + multi-write detection tests for ParallelScheduler (Task 05).

Covers the Task 05 acceptance criteria:

- `InvalidUpdateError(GraphBubbleUp)` exception class.
- `LastValue.update(values: list[T])` raises `InvalidUpdateError` when
  `len(values) > 1`.
- Multi-write detection only triggers under actual concurrency (single-node
  fast path keeps `values` length at 1).
- `ParallelScheduler` fork logic: when the ready batch has multiple instances
  OR there are RUNNING instances, each READY instance forks `main_state`
  before execution; `instance.forked_state` is set to the snapshot.
- `GraphContext.state` points to `forked_state` under fork mode; to
  `main_state` under fast path.
- After instance completion, `NodeResult.state_update` merges back to
  `main_state` via the atomic `commit + apply_state_update + advance +
  complete` segment (generation-based conflict detection); imperative
  mutations do NOT propagate.
- Multiple concurrent instances writing the same `LastValue` field raise
  `InvalidUpdateError`; `ReducerChannel` fields fold correctly.
- Single-node fast path skips fork.
- Map-reduce parallel pattern: `ReducerChannel` correctly folds.
"""

from __future__ import annotations

from typing import Annotated, Any

import pytest
from helpers import make_coordinator, make_runtime

from modex_graph import (
    Graph,
    GraphBubbleUp,
    GraphContext,
    GraphEngine,
    GraphNode,
    GraphState,
    IntegratedInput,
    InvalidUpdateError,
    LastValue,
    Node,
    NodeInstanceStatus,
    NodeResult,
    ParallelScheduler,
    ReducerChannel,
    SchedulerKind,
)

# ── Shared test state ──────────────────────────────────────────────────────


class ForkState(GraphState):
    """State with LastValue + ReducerChannel fields for fork isolation tests."""

    count: Annotated[int, LastValue] = 0
    name: Annotated[str, LastValue] = "init"
    items: Annotated[list[str], ReducerChannel(reducer=lambda a, b: a + b)] = []
    squares: Annotated[list[int], ReducerChannel(reducer=lambda a, b: a + b)] = []


def make_parallel_ctx(state: ForkState | None = None) -> GraphContext[ForkState]:
    return GraphContext(
        state=state if state is not None else ForkState(),
        runtime=make_runtime(),
        coordinator=make_coordinator(),
        scheduler_kind=SchedulerKind.PARALLEL,
    )


# ── Test nodes ─────────────────────────────────────────────────────────────


class FanOutNode(Node[ForkState]):
    """Dispatches to two targets, creating concurrent instances."""

    def __init__(self, target_a: str = "b", target_b: str = "c") -> None:
        self.target_a = target_a
        self.target_b = target_b

    def execute(
        self, ctx: GraphContext[ForkState], integrated_input: IntegratedInput
    ) -> NodeResult:
        self.deliver(None, self.target_a, ctx)
        self.deliver(None, self.target_b, ctx)
        return NodeResult()


class WriteLastValueNode(Node[ForkState]):
    """Returns state_update writing to a LastValue field."""

    def __init__(self, field: str, value: Any) -> None:
        self.field = field
        self.value = value

    def execute(
        self, ctx: GraphContext[ForkState], integrated_input: IntegratedInput
    ) -> NodeResult:
        self.deliver(None, None, ctx)
        return NodeResult(state_update={self.field: self.value})


class WriteReducerNode(Node[ForkState]):
    """Returns state_update writing a list to a ReducerChannel field."""

    def __init__(self, field: str, value: list[Any]) -> None:
        self.field = field
        self.value = value

    def execute(
        self, ctx: GraphContext[ForkState], integrated_input: IntegratedInput
    ) -> NodeResult:
        self.deliver(None, None, ctx)
        return NodeResult(state_update={self.field: self.value})


class ImperativeMutateNode(Node[ForkState]):
    """Imperatively mutates a field (no state_update)."""

    def __init__(self, field: str, value: Any, target: str | None = None) -> None:
        self.field = field
        self.value = value
        self.target = target

    def execute(
        self, ctx: GraphContext[ForkState], integrated_input: IntegratedInput
    ) -> NodeResult:
        setattr(ctx.state, self.field, self.value)
        if self.target is not None:
            self.deliver(None, self.target, ctx)
        return NodeResult()


class DispatchToEndNode(Node[ForkState]):
    """No-op node that dispatches to END."""

    def execute(
        self, ctx: GraphContext[ForkState], integrated_input: IntegratedInput
    ) -> NodeResult:
        self.deliver(None, GraphNode.END, ctx)
        return NodeResult()


class SquareWorker(Node[ForkState]):
    """Writes [item*item] to the squares ReducerChannel."""

    def __init__(self, item: int) -> None:
        self.item = item

    def execute(
        self, ctx: GraphContext[ForkState], integrated_input: IntegratedInput
    ) -> NodeResult:
        self.deliver(None, None, ctx)
        return NodeResult(state_update={"squares": [self.item * self.item]})


# ── InvalidUpdateError hierarchy ───────────────────────────────────────────


class TestInvalidUpdateErrorHierarchy:
    """InvalidUpdateError is a GraphBubbleUp subclass."""

    def test_is_graphbubbleup_subclass(self) -> None:
        assert issubclass(InvalidUpdateError, GraphBubbleUp)

    def test_can_be_raised_and_caught_as_graphbubbleup(self) -> None:
        with pytest.raises(GraphBubbleUp):
            raise InvalidUpdateError("multi-write")

    def test_carries_message(self) -> None:
        exc = InvalidUpdateError("concurrent writes to LastValue")
        assert "concurrent writes" in str(exc)


# ── LastValue multi-write detection (unit-level) ───────────────────────────


class TestLastValueMultiWrite:
    """LastValue.update raises InvalidUpdateError when len(values) > 1."""

    def test_single_value_update_succeeds(self) -> None:
        ch = LastValue()._fresh(int)
        ch.update([42])
        assert ch.get() == 42

    def test_empty_values_is_noop(self) -> None:
        ch = LastValue()._fresh(int)
        ch.set(0)
        ch.update([])
        assert ch.get() == 0

    def test_multi_value_raises_invalidupdateerror(self) -> None:
        ch = LastValue()._fresh(int)
        with pytest.raises(InvalidUpdateError, match="2.*concurrent"):
            ch.update([1, 2])

    def test_three_values_raises(self) -> None:
        ch = LastValue()._fresh(int)
        with pytest.raises(InvalidUpdateError):
            ch.update([1, 2, 3])


# ── ReducerChannel concurrent fold (unit-level) ────────────────────────────


class TestReducerChannelConcurrentFold:
    """ReducerChannel folds multiple concurrent writes in order."""

    def test_fold_two_values(self) -> None:
        ch = ReducerChannel(reducer=lambda a, b: a + b)._fresh(list)
        ch.set([])
        ch.update([["a"], ["b"]])
        assert ch.get() == ["a", "b"]

    def test_fold_three_values(self) -> None:
        ch = ReducerChannel(reducer=lambda a, b: a + b)._fresh(list)
        ch.set([])
        ch.update([["a"], ["b"], ["c"]])
        assert ch.get() == ["a", "b", "c"]


# ── GraphState.apply_concurrent_updates ────────────────────────────────────


class TestApplyConcurrentUpdates:
    """GraphState.apply_concurrent_updates groups by field and folds together."""

    def test_single_update_equivalent_to_apply_state_update(self) -> None:
        state = ForkState()
        state.apply_concurrent_updates([{"count": 5}])
        assert state.count == 5

    def test_multiple_fields_in_one_update(self) -> None:
        state = ForkState()
        state.apply_concurrent_updates([{"count": 1, "name": "x"}])
        assert state.count == 1
        assert state.name == "x"

    def test_reducer_channel_folds_multiple_updates(self) -> None:
        state = ForkState()
        state.apply_concurrent_updates([{"items": ["a"]}, {"items": ["b"]}, {"items": ["c"]}])
        assert state.items == ["a", "b", "c"]

    def test_lastvalue_multi_write_raises(self) -> None:
        state = ForkState()
        with pytest.raises(InvalidUpdateError):
            state.apply_concurrent_updates([{"count": 1}, {"count": 2}])

    def test_lastvalue_single_write_succeeds(self) -> None:
        state = ForkState()
        state.apply_concurrent_updates([{"count": 1}, {"name": "ok"}])
        assert state.count == 1
        assert state.name == "ok"

    def test_unknown_field_raises_keyerror(self) -> None:
        state = ForkState()
        with pytest.raises(KeyError, match="nonexistent"):
            state.apply_concurrent_updates([{"nonexistent": 1}])

    def test_empty_updates_list_is_noop(self) -> None:
        state = ForkState(count=5, name="keep")
        state.apply_concurrent_updates([])
        assert state.count == 5
        assert state.name == "keep"


# ── Fork isolation under ParallelScheduler ─────────────────────────────────


class TestForkIsolationMultiWrite:
    """Two concurrent instances writing the same LastValue field raise InvalidUpdateError."""

    async def test_two_concurrent_writes_same_lastvalue_raises(self) -> None:
        g: Graph[ForkState] = Graph()
        g.add_node("a", FanOutNode(target_a="b", target_b="c"))
        g.add_node("b", WriteLastValueNode(field="count", value=1))
        g.add_node("c", WriteLastValueNode(field="count", value=2))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", "b")
        g.add_edge("a", "c")
        g.add_edge("b", GraphNode.END)
        g.add_edge("c", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = make_parallel_ctx(ForkState(count=0))
        with pytest.raises(InvalidUpdateError):
            await GraphEngine(compiled).run_async(ctx)

    async def test_two_concurrent_writes_different_lastvalue_fields_succeeds(self) -> None:
        g: Graph[ForkState] = Graph()
        g.add_node("a", FanOutNode(target_a="b", target_b="c"))
        g.add_node("b", WriteLastValueNode(field="count", value=10))
        g.add_node("c", WriteLastValueNode(field="name", value="from_c"))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", "b")
        g.add_edge("a", "c")
        g.add_edge("b", GraphNode.END)
        g.add_edge("c", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = make_parallel_ctx(ForkState())
        result = await GraphEngine(compiled).run_async(ctx)
        assert result.count == 10
        assert result.name == "from_c"


class TestForkIsolationReducerFold:
    """Two concurrent instances writing the same ReducerChannel field fold correctly."""

    async def test_two_concurrent_writes_same_reducer_folds(self) -> None:
        g: Graph[ForkState] = Graph()
        g.add_node("a", FanOutNode(target_a="b", target_b="c"))
        g.add_node("b", WriteReducerNode(field="items", value=["from_b"]))
        g.add_node("c", WriteReducerNode(field="items", value=["from_c"]))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", "b")
        g.add_edge("a", "c")
        g.add_edge("b", GraphNode.END)
        g.add_edge("c", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = make_parallel_ctx(ForkState(items=[]))
        result = await GraphEngine(compiled).run_async(ctx)
        # Both contributions folded in execution order.
        assert result.items == ["from_b", "from_c"]


class TestImperativeMutationNonPropagation:
    """Imperative mutations on forked state do NOT propagate to main_state."""

    async def test_imperative_mutation_under_fork_does_not_propagate(self) -> None:
        """Two concurrent instances imperatively mutate count; main_state keeps 0."""
        g: Graph[ForkState] = Graph()
        g.add_node("a", FanOutNode(target_a="b", target_b="c"))
        g.add_node("b", ImperativeMutateNode(field="count", value=999, target=GraphNode.END))
        g.add_node("c", ImperativeMutateNode(field="count", value=888, target=GraphNode.END))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", "b")
        g.add_edge("a", "c")
        g.add_edge("b", GraphNode.END)
        g.add_edge("c", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = make_parallel_ctx(ForkState(count=0))
        result = await GraphEngine(compiled).run_async(ctx)
        # Imperative mutations on forked_state do NOT reach main_state.
        assert result.count == 0

    async def test_imperative_mutation_to_reducer_does_not_propagate(self) -> None:
        """Imperative mutation on a ReducerChannel field under fork is dropped."""
        g: Graph[ForkState] = Graph()
        g.add_node("a", FanOutNode(target_a="b", target_b="c"))
        # Both imperatively overwrite items (no state_update).
        g.add_node(
            "b",
            ImperativeMutateNode(field="items", value=["poison_b"], target=GraphNode.END),
        )
        g.add_node(
            "c",
            ImperativeMutateNode(field="items", value=["poison_c"], target=GraphNode.END),
        )
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", "b")
        g.add_edge("a", "c")
        g.add_edge("b", GraphNode.END)
        g.add_edge("c", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = make_parallel_ctx(ForkState(items=["original"]))
        result = await GraphEngine(compiled).run_async(ctx)
        # No state_update contributions → items stays at original.
        assert result.items == ["original"]

    async def test_fork_creates_forked_state_for_concurrent_instances(self) -> None:
        """When batch > 1, each instance has a non-None forked_state."""
        g: Graph[ForkState] = Graph()
        g.add_node("a", FanOutNode(target_a="b", target_b="c"))
        g.add_node("b", DispatchToEndNode())
        g.add_node("c", DispatchToEndNode())
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", "b")
        g.add_edge("a", "c")
        g.add_edge("b", GraphNode.END)
        g.add_edge("c", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = make_parallel_ctx(ForkState())
        engine = GraphEngine(compiled)
        await engine.run_async(ctx)

        scheduler = engine._scheduler
        assert isinstance(scheduler, ParallelScheduler)
        b_inst = scheduler._instances["b#1"]
        c_inst = scheduler._instances["c#2"]
        assert b_inst.forked_state is not None
        assert c_inst.forked_state is not None
        # Forked states are independent objects (not main_state).
        main_state = ctx.state
        assert b_inst.forked_state is not main_state
        assert c_inst.forked_state is not main_state
        assert b_inst.forked_state is not c_inst.forked_state


class TestFastPathImperativeMutation:
    """Single-node fast path: imperative mutation is directly effective."""

    async def test_fast_path_imperative_mutation_direct(self) -> None:
        """Single instance → fast path → ctx.state IS main_state → mutation sticks."""
        g: Graph[ForkState] = Graph()
        g.add_node("a", ImperativeMutateNode(field="count", value=42, target=GraphNode.END))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = make_parallel_ctx(ForkState(count=0))
        result = await GraphEngine(compiled).run_async(ctx)
        assert result.count == 42

    async def test_fast_path_forked_state_is_none(self) -> None:
        """Fast path: instance.forked_state is None (no fork)."""
        g: Graph[ForkState] = Graph()
        g.add_node("a", ImperativeMutateNode(field="count", value=7, target=GraphNode.END))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = make_parallel_ctx(ForkState(count=0))
        engine = GraphEngine(compiled)
        await engine.run_async(ctx)

        scheduler = engine._scheduler
        assert isinstance(scheduler, ParallelScheduler)
        inst = scheduler._instances["a#0"]
        assert inst.forked_state is None
        assert inst.status == NodeInstanceStatus.COMPLETED

    async def test_fast_path_state_update_applied_directly(self) -> None:
        """Fast path: state_update applied directly to main_state."""
        g: Graph[ForkState] = Graph()
        g.add_node("a", WriteLastValueNode(field="count", value=99))
        g.add_node("b", DispatchToEndNode())
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", "b")
        g.add_edge("b", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = make_parallel_ctx(ForkState(count=0))
        result = await GraphEngine(compiled).run_async(ctx)
        # A's state_update was applied to main_state directly.
        assert result.count == 99


class TestFastPathSkipsFork:
    """Single-node fast path skips fork — executes directly on main_state."""

    async def test_single_ready_no_running_uses_fast_path(self) -> None:
        """Linear chain A→B→END: each batch has exactly one READY → fast path throughout."""
        g: Graph[ForkState] = Graph()
        g.add_node("a", ImperativeMutateNode(field="count", value=1, target="b"))
        g.add_node("b", ImperativeMutateNode(field="count", value=2, target=GraphNode.END))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", "b")
        g.add_edge("b", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = make_parallel_ctx(ForkState(count=0))
        engine = GraphEngine(compiled)
        await engine.run_async(ctx)

        scheduler = engine._scheduler
        assert isinstance(scheduler, ParallelScheduler)
        # Both instances had forked_state=None (fast path).
        assert scheduler._instances["a#0"].forked_state is None
        assert scheduler._instances["b#1"].forked_state is None
        # Mutations accumulated directly on main_state.
        assert ctx.state.count == 2  # last mutation wins (imperative)


# ── Map-reduce parallel pattern ────────────────────────────────────────────


class TestMapReduceParallelFold:
    """Map-reduce pattern under ParallelScheduler: ReducerChannel correctly folds."""

    async def test_two_workers_fold_squares(self) -> None:
        """Two concurrent workers each contribute [item*item] to squares."""
        g: Graph[ForkState] = Graph()
        g.add_node("a", FanOutNode(target_a="b", target_b="c"))
        g.add_node("b", SquareWorker(item=3))
        g.add_node("c", SquareWorker(item=5))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", "b")
        g.add_edge("a", "c")
        g.add_edge("b", GraphNode.END)
        g.add_edge("c", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = make_parallel_ctx(ForkState(squares=[]))
        result = await GraphEngine(compiled).run_async(ctx)
        # 3^2=9, 5^2=25 folded: [9, 25]
        assert result.squares == [9, 25]

    async def test_three_workers_fold_squares(self) -> None:
        """Three concurrent workers contribute to squares ReducerChannel."""

        class TripleFanOutNode(Node[ForkState]):
            def execute(
                self, ctx: GraphContext[ForkState], integrated_input: IntegratedInput
            ) -> NodeResult:
                self.deliver(None, "b", ctx)
                self.deliver(None, "c", ctx)
                self.deliver(None, "d", ctx)
                return NodeResult()

        g: Graph[ForkState] = Graph()
        g.add_node("a", TripleFanOutNode())
        g.add_node("b", SquareWorker(item=2))
        g.add_node("c", SquareWorker(item=4))
        g.add_node("d", SquareWorker(item=6))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", "b")
        g.add_edge("a", "c")
        g.add_edge("a", "d")
        g.add_edge("b", GraphNode.END)
        g.add_edge("c", GraphNode.END)
        g.add_edge("d", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = make_parallel_ctx(ForkState(squares=[]))
        result = await GraphEngine(compiled).run_async(ctx)
        # 4, 16, 36 folded in execution order.
        assert result.squares == [4, 16, 36]


# ── Mixed: imperative + state_update under fork ────────────────────────────


class TestForkMixedMutation:
    """Under fork, imperative mutations are dropped; state_updates merge."""

    async def test_imperative_dropped_but_state_update_merges(self) -> None:
        """Worker imperatively mutates count AND returns state_update for items.

        The imperative count mutation should NOT propagate; the items
        state_update SHOULD merge back to main_state.
        """

        class MixedNode(Node[ForkState]):
            def __init__(self, poison_count: int, item_label: str) -> None:
                self.poison_count = poison_count
                self.item_label = item_label

            def execute(
                self, ctx: GraphContext[ForkState], integrated_input: IntegratedInput
            ) -> NodeResult:
                # Imperative mutation — should NOT propagate.
                ctx.state.count = self.poison_count
                # Declarative state_update — should merge.
                self.deliver(None, None, ctx)
                return NodeResult(
                    state_update={"items": [self.item_label]},
                )

        g: Graph[ForkState] = Graph()
        g.add_node("a", FanOutNode(target_a="b", target_b="c"))
        g.add_node("b", MixedNode(poison_count=999, item_label="b_item"))
        g.add_node("c", MixedNode(poison_count=888, item_label="c_item"))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", "b")
        g.add_edge("a", "c")
        g.add_edge("b", GraphNode.END)
        g.add_edge("c", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = make_parallel_ctx(ForkState(count=0, items=[]))
        result = await GraphEngine(compiled).run_async(ctx)
        # Imperative count mutations dropped.
        assert result.count == 0
        # Declarative items state_updates folded.
        assert result.items == ["b_item", "c_item"]


# ── Fork isolation: downstream sees merged state ──────────────────────────


class TestForkDownstreamSeesMergedState:
    """After fork batch completes, downstream instances see merged main_state."""

    async def test_downstream_forks_from_merged_state(self) -> None:
        """A fans out to B, C (write state_update to ReducerChannel).
        B dispatches to D; C dispatches to END. After the fork batch,
        items are merged to main_state. D runs in the next batch (fast
        path, single instance) and reads the merged items from main_state.
        """

        class WriteAndDispatchNode(Node[ForkState]):
            def __init__(self, item: str, target: str) -> None:
                self.item = item
                self.target = target

            def execute(
                self, ctx: GraphContext[ForkState], integrated_input: IntegratedInput
            ) -> NodeResult:
                self.deliver(None, None, ctx)
                return NodeResult(state_update={"items": [self.item]})

        class ReadItemsNode(Node[ForkState]):
            """Reads ctx.state.items and writes name imperatively (fast path)."""

            def execute(
                self, ctx: GraphContext[ForkState], integrated_input: IntegratedInput
            ) -> NodeResult:
                items = list(ctx.state.items)
                ctx.state.name = ",".join(items)
                self.deliver(None, GraphNode.END, ctx)
                return NodeResult()

        g: Graph[ForkState] = Graph()
        g.add_node("a", FanOutNode(target_a="b", target_b="c"))
        g.add_node("b", WriteAndDispatchNode(item="x", target="d"))
        g.add_node("c", WriteAndDispatchNode(item="y", target=GraphNode.END))
        g.add_node("d", ReadItemsNode())
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", "b")
        g.add_edge("a", "c")
        g.add_edge("b", "d")
        g.add_edge("b", GraphNode.END)
        g.add_edge("c", GraphNode.END)
        g.add_edge("d", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = make_parallel_ctx(ForkState(items=[]))
        result = await GraphEngine(compiled).run_async(ctx)
        # B, C merged: items = ["x", "y"].
        assert result.items == ["x", "y"]
        # D (fast path) read merged items and wrote name directly to main_state.
        assert result.name == "x,y"
