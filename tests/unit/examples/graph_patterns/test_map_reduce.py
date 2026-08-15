# ruff: noqa: ANN401
"""Tests for ``examples/graph_patterns/map_reduce.py``.

Verifies the pure-deliver MapReduce dataflow:
- ``MapNode`` delivers each item to the worker node (fan-out).
- Workers deliver squared results to ``"reduce"`` (no shared state writes).
- ``ReduceNode`` reads ``IntegratedInput.payloads``, applies the reducer,
  and delivers the reduced result to END.
- A complete split -> fan-out -> reduce graph produces the expected final
  reduced value delivered to END.

``SqState`` inherits ``DefaultGraphState`` so ``EndNode`` writes the
END-delivered result to ``ctx.state.result``. Tests assert on
``result.result[0].content`` (the reduced value, stringified by
``GraphPayload``).
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from importlib import import_module
from pathlib import Path
from typing import Any

from modex_graph import (
    DefaultGraphState,
    Graph,
    GraphContext,
    GraphEngine,
    GraphNode,
    GraphPersistenceCoordinator,
    GraphRuntime,
    IntegratedInput,
    Node,
    NullDeliverStoreFactory,
    NullGraphInstanceStore,
    NullNodeStateStore,
)
from modex_graph.scheduler.bootstrap import BootstrapMode

_EXAMPLES_DIR = Path(__file__).parent.parent.parent.parent.parent / "examples"
if str(_EXAMPLES_DIR) not in sys.path:
    sys.path.insert(0, str(_EXAMPLES_DIR))

_map_reduce = import_module("graph_patterns.map_reduce")
MapNode = _map_reduce.MapNode
ReduceNode = _map_reduce.ReduceNode
build_map_reduce_graph = _map_reduce.build_map_reduce_graph


class _AutoRegCoord(GraphPersistenceCoordinator):
    def collect_consumable_delivers(
        self, node_id: str, invocation_id: int
    ) -> list[Any]:
        if self.get_deliver_store(node_id) is None:
            self.register_node(node_id)
        return super().collect_consumable_delivers(node_id, invocation_id)

    def route_deliver(
        self,
        target_node_id: str,
        content: Any,
        source_node_id: str,
        source_invocation_id: int,
        source_node_name: str | None = None,
        stage: bool = False,
    ) -> int | None:
        if self.get_deliver_store(target_node_id) is None:
            self.register_node(target_node_id)
        return super().route_deliver(
            target_node_id,
            content,
            source_node_id,
            source_invocation_id,
            source_node_name,
            stage,
        )


def _make_coordinator() -> _AutoRegCoord:
    return _AutoRegCoord(
        graph_instance_id=0,
        instance_store=NullGraphInstanceStore(),
        node_state_store=NullNodeStateStore(0),
        default_deliver_store_factory=NullDeliverStoreFactory(),
    )


class SqState(DefaultGraphState):
    """State for the square-and-sum map-reduce example.

    Only ``items`` is needed — squared values flow through delivers, not
    shared state. Inherits ``result`` from ``DefaultGraphState`` so
    ``EndNode`` writes the reduced value to ``ctx.state.result``.
    """

    items: list[int] = []


class SquareWorker(Node[SqState]):
    """Square each item from MapNode and deliver to reduce.

    For the empty-items sentinel (``content is None``), delivers ``0`` so
    reduce still fires and produces ``sum([0]) == 0``.
    """

    async def execute(self, ctx: GraphContext[SqState], integrated_input: IntegratedInput) -> None:
        for payload in integrated_input.payloads:
            item = payload.content
            self.deliver(item * item if item is not None else 0, "reduce", ctx)
        return None


def _make_ctx(state: SqState | None = None) -> GraphContext[SqState]:
    return GraphContext(
        state=state if state is not None else SqState(),
        runtime=GraphRuntime(),
        coordinator=_make_coordinator(),
    )


def _target_contents(ctx: GraphContext[SqState], target: str) -> list[Any]:
    store = ctx.coordinator.get_deliver_store(target)
    assert store is not None
    return [record.content for record in store.query_consumable(0, target)]


def _result_value(state: SqState) -> int:
    assert state.result is not None
    return int(state.result[0].content)


def _items(state: SqState) -> list[int]:
    return state.items


def _build_graph(
    *,
    reducer: Callable[[list[int]], int] = sum,
) -> Graph[SqState]:
    return build_map_reduce_graph(
        items_fn=_items,
        worker_node=SquareWorker(),
        reducer=reducer,
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
        assert _target_contents(ctx, "worker") == [1, 2, 3, 4, 5]

    async def test_empty_items_still_delivers(self) -> None:
        """items_fn returns [] -> delivers single None to worker (ensures worker fires)."""
        map_node = MapNode(
            items_fn=_items,
            worker_node="worker",
        )
        ctx = _make_ctx(SqState(items=[]))
        await map_node.run(ctx)
        assert _target_contents(ctx, "worker") == [None]


class TestWorkerAccumulation:
    async def test_folds_worker_contributions_in_execution_order(self) -> None:
        """Worker squares [3, 1, 2] -> reduce sums = 9 + 1 + 4 = 14."""
        compiled = _build_graph().compile()
        ctx = _make_ctx(SqState(items=[3, 1, 2]))
        result = await GraphEngine(compiled).run_async(ctx, mode=BootstrapMode.FRESH)
        assert _result_value(result) == 14


class TestReduceNode:
    """ReduceNode reads IntegratedInput.payloads, applies reducer, delivers to END."""

    async def test_reads_payloads_applies_reducer_delivers_to_end(self) -> None:
        """Isolated: feed IntegratedInput, call execute, check pending delivers."""
        reduce_node = ReduceNode(reducer=sum)
        ctx = _make_ctx()
        reduce_node.name = "reduce"
        reduce_node.node_id = "reduce"
        ctx.coordinator.register_node(reduce_node.node_id)
        for content in (1, 4, 9):
            ctx.coordinator.route_deliver(reduce_node.node_id, content, "worker", 0)

        await reduce_node.run(ctx)

        assert _target_contents(ctx, GraphNode.END) == [14]

    async def test_complete_graph_writes_expected_total_with_sum(self) -> None:
        """Complete graph: reduce sums [1, 4, 9, 16] = 30."""
        compiled = _build_graph().compile()
        ctx = _make_ctx(SqState(items=[1, 2, 3, 4]))
        result = await GraphEngine(compiled).run_async(ctx, mode=BootstrapMode.FRESH)
        assert _result_value(result) == 30

    async def test_complete_graph_writes_expected_total_with_max(self) -> None:
        """reducer=max -> max([1, 25, 4]) = 25."""
        compiled = _build_graph(reducer=max).compile()
        ctx = _make_ctx(SqState(items=[1, 5, 2]))
        result = await GraphEngine(compiled).run_async(ctx, mode=BootstrapMode.FRESH)
        assert _result_value(result) == 25


class TestCompleteMapReduceGraph:
    """Complete split -> fan-out -> reduce graph produces expected reduced value."""

    async def test_squares_then_sum(self) -> None:
        """map [1,2,3] -> square each -> sum the squares = 14."""
        compiled = _build_graph().compile()
        ctx = _make_ctx(SqState(items=[1, 2, 3]))
        result = await GraphEngine(compiled).run_async(ctx, mode=BootstrapMode.FRESH)
        assert _result_value(result) == 14

    async def test_empty_items_produces_zero_total(self) -> None:
        """Empty input -> worker delivers 0 -> reduce sums = 0."""
        compiled = _build_graph().compile()
        ctx = _make_ctx(SqState(items=[]))
        result = await GraphEngine(compiled).run_async(ctx, mode=BootstrapMode.FRESH)
        assert _result_value(result) == 0

    async def test_single_item(self) -> None:
        """Single item -> one worker -> square -> sum = item**2."""
        compiled = _build_graph().compile()
        ctx = _make_ctx(SqState(items=[7]))
        result = await GraphEngine(compiled).run_async(ctx, mode=BootstrapMode.FRESH)
        assert _result_value(result) == 49
