"""MapReduce graph pattern over `modex_graph`.

Reusable split -> fan-out -> reduce workflow using the deliver/submit API:

- `MapNode` calls `deliver(item, worker_node, ctx)` for each item (fan-out).
- `ReducerChannel` for fan-in (folds all workers' `state_update`
  contributions).
- `ReduceNode` reads the accumulated list, applies the reducer, writes the
  result.

Under `LinearScheduler`, multiple delivers to the same target group into a
single worker execution (the worker reads items from state). Under
`ParallelScheduler`, each deliver creates a separate worker instance
receiving the item via `integrated_input`.

This is example code (lives under `examples/` per ADR-0007 rule 9). It
uses only the public `modex_graph` API — no framework-internal hooks, no
`modex_agent` imports. The pattern is generic over any `GraphState`
subclass.

Topology::

    START -> map -> worker -> reduce -> END
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from modex_graph import (
    Graph,
    GraphContext,
    GraphNode,
    GraphState,
    IntegratedInput,
    Node,
    NodeResult,
)


class MapNode[S: GraphState](Node[S]):
    """Fan-out node: delivers each item to the worker node.

    `execute` reads `items_fn(ctx.state)` to get a list of items, then
    calls `deliver(item, worker_node, ctx)` for each. Under
    `ParallelScheduler`, each deliver creates a separate worker instance.

    Example::

        g.add_node("map", MapNode(
            items_fn=lambda s: s.items,
            worker_node="worker",
        ))
    """

    def __init__(
        self,
        items_fn: Callable[[S], list[Any]],
        worker_node: str,
    ) -> None:
        self.items_fn = items_fn
        self.worker_node = worker_node

    def execute(self, ctx: GraphContext[S], integrated_input: IntegratedInput) -> NodeResult:
        items = self.items_fn(ctx.state)
        if items:
            for item in items:
                self.deliver(item, self.worker_node, ctx)
        else:
            self.deliver(None, "reduce", ctx)
        return NodeResult()


class ReduceNode[S: GraphState](Node[S]):
    """Fan-in node: reads accumulated list, applies reducer, writes result.

    `execute` reads `getattr(ctx.state, source_field)` (the
    `ReducerChannel`-backed field that has accumulated all workers'
    `state_update` contributions), applies `reducer(values)`, and writes
    the result to `result_field` via `setattr(ctx.state, result_field,
    reduced_value)`.

    Imperative mode — `ReduceNode` does NOT use `NodeResult.state_update`
    because it is the terminal node writing the final result. The
    `getattr`/`setattr` dynamic field access is a legitimate extension
    boundary (rule 6): `ReduceNode` is generic over any `GraphState`
    subclass and any field names.

    Example::

        g.add_node("reduce", ReduceNode(
            reducer=sum,
            source_field="squares",
            result_field="total",
        ))
    """

    def __init__(
        self,
        reducer: Callable[[list[Any]], Any],
        source_field: str,
        result_field: str,
    ) -> None:
        self.reducer = reducer
        self.source_field = source_field
        self.result_field = result_field

    def execute(self, ctx: GraphContext[S], integrated_input: IntegratedInput) -> NodeResult:
        values = getattr(ctx.state, self.source_field)
        reduced = self.reducer(values)
        setattr(ctx.state, self.result_field, reduced)
        self.deliver(None, GraphNode.END, ctx)
        return NodeResult()


def build_map_reduce_graph[S: GraphState](
    items_fn: Callable[[S], list[Any]],
    worker_node: Node[S],
    reducer: Callable[[list[Any]], Any],
    source_field: str,
    result_field: str,
) -> Graph[S]:
    """Build a split -> fan-out -> reduce topology.

    Topology::

        START -> map -> worker -> reduce -> END

    - ``map`` is a `MapNode` that delivers each item to ``worker``.
    - ``worker`` processes items and returns
      `NodeResult(state_update={source_field: [processed_result]})`.
      Under `LinearScheduler`, one execution processes all items; under
      `ParallelScheduler`, one instance per item.
    - ``reduce`` is a `ReduceNode` that reads the `ReducerChannel`-backed
      `source_field`, applies `reducer(values)`, and writes `result_field`.

    The worker node is registered as ``"worker"``; `MapNode`'s
    `worker_node` parameter is wired to ``"worker"`` automatically.

    Returns the uncompiled `Graph[S]` — call `.compile()` then pass to
    `GraphEngine` to execute.
    """
    g: Graph[S] = Graph()
    g.add_node("map", MapNode(items_fn=items_fn, worker_node="worker"))
    g.add_node("worker", worker_node)
    g.add_node(
        "reduce",
        ReduceNode(
            reducer=reducer,
            source_field=source_field,
            result_field=result_field,
        ),
    )
    g.add_edge(GraphNode.START, "map")
    g.add_edge("map", "worker")
    g.add_edge("map", "reduce")
    g.add_edge("worker", "reduce")
    g.add_edge("reduce", GraphNode.END)
    return g


__all__ = [
    "MapNode",
    "ReduceNode",
    "build_map_reduce_graph",
]
