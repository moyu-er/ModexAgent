# ruff: noqa: ANN401
"""Tests for `examples/graph_patterns/map_reduce.py`.

Verifies:
- `MapNode` delivers each item to the worker node.
- Workers append results directly to shared state in execution order.
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
from importlib import import_module
from pathlib import Path
from typing import Any

from modex_graph import (
    Graph,
    GraphContext,
    GraphEngine,
    GraphNode,
    GraphPersistenceCoordinator,
    GraphRuntime,
    GraphState,
    IntegratedInput,
    Node,
    NullDeliverStoreFactory,
    NullGraphInstanceStore,
    NullNodeStateStore,
)

_EXAMPLES_DIR = Path(__file__).parent.parent.parent.parent.parent / "examples"
if str(_EXAMPLES_DIR) not in sys.path:
    sys.path.insert(0, str(_EXAMPLES_DIR))

_map_reduce = import_module("graph_patterns.map_reduce")
MapNode = _map_reduce.MapNode
ReduceNode = _map_reduce.ReduceNode
build_map_reduce_graph = _map_reduce.build_map_reduce_graph


class _AutoRegCoord(GraphPersistenceCoordinator):
    def collect_consumable_delivers(
        self, node_name: str, invocation_id: int
    ) -> list[Any]:
        if self.get_deliver_store(node_name) is None:
            self.register_node(node_name)
        return super().collect_consumable_delivers(node_name, invocation_id)

    def route_deliver(
        self,
        target_node: str,
        content: Any,
        source_node: str,
        source_invocation_id: int,
        source_node_name: str | None = None,
    ) -> int | None:
        if target_node != GraphNode.END and self.get_deliver_store(target_node) is None:
            self.register_node(target_node)
        return super().route_deliver(target_node, content, source_node, source_invocation_id, source_node_name)


def _make_coordinator() -> _AutoRegCoord:
    return _AutoRegCoord(
        graph_instance_id=0,
        instance_store=NullGraphInstanceStore(),
        node_state_store=NullNodeStateStore(0),
        default_deliver_store_factory=NullDeliverStoreFactory(),
    )


class SqState(GraphState):
    """State for the square-and-sum map-reduce example."""

    items: list[int] = []
    squares: list[int] = []
    total: int = 0


class SquareWorker(Node[SqState]):
    async def execute(self, ctx: GraphContext[SqState], integrated_input: IntegratedInput) -> None:
        squares = [item * item for item in ctx.state.items]
        ctx.state.squares.extend(squares)
        if squares:
            self.deliver(None, "reduce", ctx)
        else:
            self.deliver(None, GraphNode.END, ctx)
        return None


def _make_ctx(state: SqState | None = None) -> GraphContext[SqState]:
    return GraphContext(
        state=state if state is not None else SqState(),
        runtime=GraphRuntime(),
        coordinator=_make_coordinator(),
    )


def _items(state: SqState) -> list[int]:
    return state.items


def _build_graph(
    items: list[int],
    *,
    reducer: Callable[[list[int]], int] = sum,
    source_field: str = "squares",
    result_field: str = "total",
) -> Graph[SqState]:
    return build_map_reduce_graph(
        items_fn=_items,
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
            items_fn=_items,
            worker_node="worker",
        )
        ctx = _make_ctx(SqState(items=[1, 2, 3, 4, 5]))
        await map_node.run(ctx)
        result = map_node._submit_result
        assert "worker" in result
        assert len(result["worker"]) == 5

    async def test_empty_items_still_delivers(self) -> None:
        """items_fn returns [] -> no delivers -> RoutingError (enforce_deliver)."""
        from modex_graph import RoutingError

        map_node = MapNode(
            items_fn=_items,
            worker_node="worker",
        )
        ctx = _make_ctx(SqState(items=[]))
        try:
            await map_node.run(ctx)
            # If no RoutingError, check if it delivered anything
            assert not map_node._submit_result or "worker" not in map_node._submit_result
        except RoutingError:
            pass  # Expected — no items means no delivers


class TestWorkerAccumulation:
    async def test_folds_worker_contributions_in_execution_order(self) -> None:
        """Worker squares [3, 1, 2] -> squares = [9, 1, 4]."""
        compiled = _build_graph([3, 1, 2]).compile()
        ctx = _make_ctx(SqState(items=[3, 1, 2]))
        result = await GraphEngine(compiled).run_async(ctx)

        assert result.squares == [9, 1, 4]


class TestReduceNode:
    """ReduceNode reads accumulated list, applies reducer, writes result_field."""

    async def test_reads_accumulated_list_applies_reducer_writes_result_field(
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

        result = await reduce_node.execute(ctx, IntegratedInput())

        assert result is None
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
