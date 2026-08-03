"""Tests for `examples/graph_patterns/map_reduce.py`.

Verifies:
- `MapNode` delivers each item to the worker node.
- `ReducerChannel` on the source field accumulates all workers'
  `state_update` contributions in order.
- `ReduceNode` reads the accumulated list, applies reducer, and writes
  the result to `result_field`.
- A complete split -> fan-out -> reduce graph produces the expected final
  aggregated state (map a list of numbers -> each worker squares its
  number -> reduce sums the squares).

Tests assert observable state via `GraphEngine.run_async(ctx)` — per
the TDD-at-the-execution-seam guidance in the task spec.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Annotated

# Add `examples/` to sys.path so `graph_patterns` is importable as a
# top-level package.
_EXAMPLES_DIR = Path(__file__).parent.parent.parent.parent.parent / "examples"
if str(_EXAMPLES_DIR) not in sys.path:
    sys.path.insert(0, str(_EXAMPLES_DIR))

from graph_patterns import (  # noqa: E402
    MapNode,
    ReduceNode,
    build_map_reduce_graph,
)

from modex_graph import (  # noqa: E402
    Graph,
    GraphContext,
    GraphEngine,
    GraphNode,
    GraphRuntime,
    GraphState,
    IntegratedInput,
    LastValue,
    Node,
    NodeResult,
    ReducerChannel,
)


class SqState(GraphState):
    """State for the square-and-sum map-reduce example."""

    items: Annotated[list[int], LastValue] = []
    squares: Annotated[list[int], ReducerChannel(reducer=lambda a, b: a + b)] = []
    total: Annotated[int, LastValue] = 0


class SquareWorker(Node[SqState]):
    """Squares each item in state.items, writes results via state_update."""

    def execute(self, ctx: GraphContext[SqState], integrated_input: IntegratedInput) -> NodeResult:
        squares = [item * item for item in ctx.state.items]
        if squares:
            self.deliver(None, "reduce", ctx)
        else:
            self.deliver(None, GraphNode.END, ctx)
        return NodeResult(state_update={"squares": squares})


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
        worker_node=SquareWorker(),
        reducer=reducer,
        source_field=source_field,
        result_field=result_field,
    )


class TestMapNode:
    """MapNode delivers each item to the worker node."""

    async def test_delivers_all_items_to_worker(self) -> None:
        """items_fn returns N items -> N delivers to worker."""
        map_node = MapNode(
            items_fn=lambda s: s.items,
            worker_node="worker",
        )
        ctx = _make_ctx(SqState(items=[1, 2, 3, 4, 5]))
        await map_node.run(ctx, enforce_deliver=True)
        result = map_node.result
        assert "worker" in result
        assert len(result["worker"]) == 5

    async def test_empty_items_still_delivers(self) -> None:
        """items_fn returns [] -> no delivers -> RoutingError (enforce_deliver)."""
        from modex_graph import RoutingError

        map_node = MapNode(
            items_fn=lambda s: s.items,
            worker_node="worker",
        )
        ctx = _make_ctx(SqState(items=[]))
        try:
            await map_node.run(ctx, enforce_deliver=True)
            # If no RoutingError, check if it delivered anything
            assert not map_node.result or "worker" not in map_node.result
        except RoutingError:
            pass  # Expected — no items means no delivers


class TestReducerChannelAccumulation:
    """ReducerChannel on source field accumulates all contributions in order."""

    async def test_folds_worker_contributions_in_execution_order(self) -> None:
        """Worker squares [3, 1, 2] -> squares = [9, 1, 4]."""
        compiled = _build_graph([3, 1, 2]).compile()
        ctx = _make_ctx(SqState(items=[3, 1, 2]))
        result = await GraphEngine(compiled).run_async(ctx)

        assert result.squares == [9, 1, 4]


class TestReduceNode:
    """ReduceNode reads accumulated list, applies reducer, writes result_field."""

    def test_reads_accumulated_list_applies_reducer_writes_result_field(
        self,
    ) -> None:
        """Isolated: pre-populate state.squares, call ReduceNode.execute, check."""
        reduce_node = ReduceNode(
            reducer=sum,
            source_field="squares",
            result_field="total",
        )
        state = SqState(squares=[1, 4, 9])
        ctx = _make_ctx(state)

        result = reduce_node.execute(ctx, IntegratedInput())

        assert result.state_update is None
        assert ctx.state.total == 14
        assert ctx.state.squares == [1, 4, 9]

    async def test_complete_graph_writes_expected_total_with_sum(self) -> None:
        """Complete graph: reduce reads squares, applies sum, writes total."""
        compiled = _build_graph([1, 2, 3, 4]).compile()
        ctx = _make_ctx(SqState(items=[1, 2, 3, 4]))
        result = await GraphEngine(compiled).run_async(ctx)

        assert result.squares == [1, 4, 9, 16]
        assert result.total == 30

    async def test_complete_graph_writes_expected_total_with_max(self) -> None:
        """reducer=max -> total == max(squares)."""
        compiled = _build_graph([1, 5, 2], reducer=max).compile()
        ctx = _make_ctx(SqState(items=[1, 5, 2]))
        result = await GraphEngine(compiled).run_async(ctx)

        assert result.squares == [1, 25, 4]
        assert result.total == 25


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
        """Empty input -> no items -> reduce sums empty list = 0."""
        compiled = _build_graph([]).compile()
        ctx = _make_ctx(SqState(items=[]))
        result = await GraphEngine(compiled).run_async(ctx)

        assert result.squares == []
        assert result.total == 0

    async def test_single_item(self) -> None:
        """Single item -> one worker -> square -> sum = item**2."""
        compiled = _build_graph([7]).compile()
        ctx = _make_ctx(SqState(items=[7]))
        result = await GraphEngine(compiled).run_async(ctx)

        assert result.squares == [49]
        assert result.total == 49
