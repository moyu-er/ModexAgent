"""Tests for `examples/graph_patterns/map_reduce.py`.

Verifies (per the task spec acceptance criteria):
- `MapNode` emits `Command(goto=list[Task])` with the correct number of
  tasks (one per item from `items_fn`).
- Each `Task` carries an independent state (constructed by `state_fn`) —
  imperative mutations in one worker do not appear in another worker's
  state.
- `ReducerChannel` on the source field accumulates all workers'
  `state_update` contributions in order.
- `ReduceNode` reads the accumulated list, applies `reducer`, and writes
  the result to `result_field`.
- A complete split -> fan-out -> reduce graph produces the expected final
  aggregated state (map a list of numbers -> each worker squares its
  number -> reduce sums the squares).

Tests assert observable state via `GraphEngine.run_async(ctx)` (and, for
the isolated `MapNode`/`ReduceNode` checks, direct `execute` calls) — per
the TDD-at-the-execution-seam guidance in the task spec.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Annotated

# Add `examples/` to sys.path so `graph_patterns` is importable as a
# top-level package. Mirrors the pattern in test_conditional.py /
# test_retry.py.
_EXAMPLES_DIR = Path(__file__).parent.parent.parent.parent.parent / "examples"
if str(_EXAMPLES_DIR) not in sys.path:
    sys.path.insert(0, str(_EXAMPLES_DIR))

from graph_patterns import (  # noqa: E402
    MapNode,
    ReduceNode,
    build_map_reduce_graph,
)

from modex_graph import (  # noqa: E402
    Command,
    Graph,
    GraphContext,
    GraphEngine,
    GraphRuntime,
    GraphState,
    LastValue,
    Node,
    NodeResult,
    ReducerChannel,
    Task,
)

# ─── Shared state type ────────────────────────────────────────────────


class SqState(GraphState):
    """State for the square-and-sum map-reduce example.

    - `items`: input list, read by `MapNode`'s `items_fn`.
    - `current_item`: the item for the current worker, set by `state_fn`
      onto each worker's independent state and read by the worker.
    - `squares`: `ReducerChannel`-backed list — accumulates each worker's
      `state_update={"squares": [x*x]}` contribution in execution order.
    - `total`: final reduced result, written imperatively by `ReduceNode`.
    """

    items: Annotated[list[int], LastValue] = []
    current_item: Annotated[int, LastValue] = 0
    squares: Annotated[list[int], ReducerChannel(reducer=lambda a, b: a + b)] = []
    total: Annotated[int, LastValue] = 0


# ─── Shared worker node ───────────────────────────────────────────────


class SquareWorker(Node[SqState]):
    """Squares `current_item`, then poisons it to detect state leakage.

    The poison (-999) is the canary: if any worker sees another worker's
    state (i.e. states are NOT independent), it would square -999 instead
    of its assigned item, producing 998001 in the `squares` list. The
    parent's `current_item` is also never mutated because imperative
    writes on a forked state do not propagate.
    """

    def execute(self, ctx: GraphContext[SqState]) -> NodeResult:
        item = ctx.state.current_item
        # Imperative mutation on the (forked) worker state — must NOT
        # propagate to parent or sibling workers.
        ctx.state.current_item = -999
        return NodeResult(state_update={"squares": [item * item]})


# ─── Helpers ──────────────────────────────────────────────────────────


def _state_fn(item: int) -> SqState:
    """Construct an independent worker state carrying one item."""
    return SqState(current_item=item)


def _make_ctx(state: SqState | None = None) -> GraphContext[SqState]:
    return GraphContext(
        state=state if state is not None else SqState(),
        runtime=GraphRuntime(),
    )


def _build_graph(
    items: list[int],
    *,
    reducer: Callable[[list[int]], int] = sum,
    source_field: str = "squares",
    result_field: str = "total",
) -> Graph[SqState]:
    return build_map_reduce_graph(
        items_fn=lambda s: s.items,
        state_fn=_state_fn,
        worker_node=SquareWorker(),
        reducer=reducer,
        source_field=source_field,
        result_field=result_field,
    )


# ─── MapNode tests ────────────────────────────────────────────────────


class TestMapNode:
    """MapNode emits Command(goto=list[Task]) with one Task per item."""

    def test_emits_command_with_correct_task_count(self) -> None:
        """items_fn returns N items -> Command.goto has N Tasks."""
        map_node = MapNode(
            items_fn=lambda s: s.items,
            worker_node="worker",
            state_fn=_state_fn,
        )
        ctx = _make_ctx(SqState(items=[1, 2, 3, 4, 5]))
        result = map_node.execute(ctx)

        assert result.command is not None
        assert isinstance(result.command, Command)
        goto = result.command.goto
        assert isinstance(goto, list)
        assert len(goto) == 5
        assert all(isinstance(t, Task) for t in goto)

    def test_each_task_targets_worker_node_and_carries_item_state(self) -> None:
        """Each Task.node == worker_node; each Task.state.current_item == item."""
        map_node = MapNode(
            items_fn=lambda s: s.items,
            worker_node="worker",
            state_fn=_state_fn,
        )
        ctx = _make_ctx(SqState(items=[10, 20, 30]))
        result = map_node.execute(ctx)

        assert result.command is not None
        tasks = result.command.goto
        assert isinstance(tasks, list)
        assert [t.node for t in tasks] == ["worker", "worker", "worker"]
        assert [t.state.current_item for t in tasks] == [10, 20, 30]

    def test_empty_items_emits_empty_task_list(self) -> None:
        """items_fn returns [] -> Command.goto is an empty list."""
        map_node = MapNode(
            items_fn=lambda s: s.items,
            worker_node="worker",
            state_fn=_state_fn,
        )
        ctx = _make_ctx(SqState(items=[]))
        result = map_node.execute(ctx)

        assert result.command is not None
        assert result.command.goto == []


# ─── Independent-state tests ──────────────────────────────────────────


class TestIndependentState:
    """Each Task carries an independent state; imperative mutations don't cross."""

    async def test_imperative_mutations_do_not_propagate_to_siblings_or_parent(
        self,
    ) -> None:
        """Worker poisons current_item=-999; next worker still sees its own item.

        Proves `state_fn` constructs independent states: each worker reads
        its OWN `current_item`, not the parent's or a sibling's. If states
        were shared, the poison (-999) would propagate and all subsequent
        workers would square -999 (producing 998001) instead of their
        assigned item. The parent's `current_item` is also unchanged
        because imperative writes on forked state do not merge back.
        """
        compiled = _build_graph([1, 2, 3]).compile()
        ctx = _make_ctx(SqState(items=[1, 2, 3]))
        result = await GraphEngine(compiled).run_async(ctx)

        # Each worker squared its OWN item (1, 2, 3) — not -999.
        assert result.squares == [1, 4, 9]
        # Parent's current_item never mutated (workers had independent states).
        assert result.current_item == 0


# ─── ReducerChannel accumulation tests ────────────────────────────────


class TestReducerChannelAccumulation:
    """ReducerChannel on source field accumulates all contributions in order."""

    async def test_folds_worker_contributions_in_execution_order(self) -> None:
        """3 workers each return state_update={squares: [x*x]} in item order.

        ReducerChannel (reducer = list concat) folds: [] + [9] + [1] + [4]
        = [9, 1, 4] — execution order is item order in Phase-a sequential
        fan-out. An order-sensitive assertion proves the accumulation
        preserves order (per the spec's order-preserving note).
        """
        compiled = _build_graph([3, 1, 2]).compile()
        ctx = _make_ctx(SqState(items=[3, 1, 2]))
        result = await GraphEngine(compiled).run_async(ctx)

        # Workers ran in item order (3, 1, 2): squares = [9, 1, 4].
        assert result.squares == [9, 1, 4]


# ─── ReduceNode tests ─────────────────────────────────────────────────


class TestReduceNode:
    """ReduceNode reads accumulated list, applies reducer, writes result_field."""

    def test_reads_accumulated_list_applies_reducer_writes_result_field(
        self,
    ) -> None:
        """Isolated: pre-populate state.squares, call ReduceNode.execute, check.

        Verifies `ReduceNode` reads `source_field` via `getattr`, applies
        `reducer(values)`, and writes `result_field` via `setattr` —
        imperative mode (no `state_update`).
        """
        reduce_node = ReduceNode(
            reducer=sum,
            source_field="squares",
            result_field="total",
        )
        # Pre-populate the state with accumulated worker contributions.
        state = SqState(squares=[1, 4, 9])
        ctx = _make_ctx(state)

        result = reduce_node.execute(ctx)

        # Imperative mode: no state_update returned.
        assert result.state_update is None
        # reducer(sum) applied to accumulated list: 1 + 4 + 9 = 14.
        assert ctx.state.total == 14
        # source_field is unchanged (reduce only writes result_field).
        assert ctx.state.squares == [1, 4, 9]

    async def test_complete_graph_writes_expected_total_with_sum(self) -> None:
        """Complete graph: reduce reads squares, applies sum, writes total."""
        compiled = _build_graph([1, 2, 3, 4]).compile()
        ctx = _make_ctx(SqState(items=[1, 2, 3, 4]))
        result = await GraphEngine(compiled).run_async(ctx)

        # 1 + 4 + 9 + 16 = 30
        assert result.squares == [1, 4, 9, 16]
        assert result.total == 30

    async def test_complete_graph_writes_expected_total_with_max(self) -> None:
        """reducer=max -> total == max(squares)."""
        compiled = _build_graph([1, 5, 2], reducer=max).compile()
        ctx = _make_ctx(SqState(items=[1, 5, 2]))
        result = await GraphEngine(compiled).run_async(ctx)

        # squares = [1, 25, 4]; max = 25
        assert result.squares == [1, 25, 4]
        assert result.total == 25


# ─── Complete graph tests ─────────────────────────────────────────────


class TestCompleteMapReduceGraph:
    """Complete split -> fan-out -> reduce graph produces expected final state."""

    async def test_squares_then_sum(self) -> None:
        """map [1,2,3] -> square each -> sum the squares = 14."""
        compiled = _build_graph([1, 2, 3]).compile()
        ctx = _make_ctx(SqState(items=[1, 2, 3]))
        result = await GraphEngine(compiled).run_async(ctx)

        assert result.squares == [1, 4, 9]
        assert result.total == 14

    async def test_empty_items_produces_zero_total(self) -> None:
        """Empty input -> no workers -> reduce sums empty list = 0."""
        compiled = _build_graph([]).compile()
        ctx = _make_ctx(SqState(items=[]))
        result = await GraphEngine(compiled).run_async(ctx)

        assert result.squares == []
        assert result.total == 0  # sum([]) == 0

    async def test_single_item(self) -> None:
        """Single item -> one worker -> square -> sum = item**2."""
        compiled = _build_graph([7]).compile()
        ctx = _make_ctx(SqState(items=[7]))
        result = await GraphEngine(compiled).run_async(ctx)

        assert result.squares == [49]
        assert result.total == 49
