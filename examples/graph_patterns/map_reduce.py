"""MapReduce graph pattern over `modex_graph`.

Pure deliver dataflow: no shared state accumulator.

- `MapNode` calls `deliver(item, worker_node, ctx)` for each item (fan-out).
  When the items list is empty, delivers a single `None` to the worker so
  the worker still fires and forwards to reduce.
- Workers must call `self.deliver(result, 'reduce', ctx)` to send results
  to Reduce. Workers do NOT write to shared state.
- `ReduceNode` reads all worker results from `IntegratedInput.payloads`,
  applies the reducer, and delivers the reduced result to END.

Under both `LinearScheduler` and `ParallelScheduler` with `ON_ALL_PREDS`
(the stable trigger), multiple delivers to the same target batch into a
single worker invocation — the worker reads all items from
`integrated_input.payloads` and delivers one result per item to reduce.
This is the recommended batch semantics: `IntegratedInput` is a batch of
causal payloads, not exactly one deliver.

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
)


class MapNode[S: GraphState](Node[S]):
    """Fan-out node: delivers each item to the worker node.

    `execute` reads `items_fn(ctx.state)` to get a list of items, then
    calls `deliver(item, worker_node, ctx)` for each. Under `ON_ALL_PREDS`
    (the stable trigger), all delivers batch into a single worker
    invocation — the worker reads items from `integrated_input.payloads`.

    When the items list is empty, delivers a single `None` to the worker
    so the worker fires and forwards to reduce — ensuring reduce always
    executes.

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

    async def execute(self, ctx: GraphContext[S], integrated_input: IntegratedInput) -> None:
        items = self.items_fn(ctx.state)
        if items:
            for item in items:
                self.deliver(item, self.worker_node, ctx)
        else:
            self.deliver(None, self.worker_node, ctx)
        return None


class ReduceNode[S: GraphState](Node[S]):
    """Fan-in node: reads worker results from IntegratedInput, applies reducer, delivers to END.

    Pure deliver dataflow: `execute` reads
    `[p.content for p in integrated_input.payloads]` to collect all worker
    results, applies `reducer(values)`, and delivers the reduced result to
    END via `self.deliver(reduced, GraphNode.END, ctx)`.

    No shared state accumulator — ReduceNode does not read from or write to
    `ctx.state`.

    Example::

        g.add_node("reduce", ReduceNode(reducer=sum))
    """

    def __init__(
        self,
        reducer: Callable[[list[Any]], Any],
    ) -> None:
        self.reducer = reducer

    async def execute(self, ctx: GraphContext[S], integrated_input: IntegratedInput) -> None:
        values = [p.content for p in integrated_input.payloads]
        reduced = self.reducer(values)
        self.deliver(reduced, GraphNode.END, ctx)
        return None


def build_map_reduce_graph[S: GraphState](
    items_fn: Callable[[S], list[Any]],
    worker_node: Node[S],
    reducer: Callable[[list[Any]], Any],
) -> Graph[S]:
    """Build a split -> fan-out -> reduce topology (pure deliver dataflow).

    Topology::

        START -> map -> worker -> reduce -> END

    - ``map`` is a `MapNode` that delivers each item to ``worker``.
    - Workers must call `self.deliver(result, 'reduce', ctx)` to send
      results to Reduce. Workers do NOT write to shared state.
      Under `ON_ALL_PREDS` (the stable trigger), all items batch into a
      single worker invocation — the worker reads items from
      `integrated_input.payloads` and delivers one result per item to
      reduce.
    - ``reduce`` reads all worker results from `IntegratedInput.payloads`,
      applies `reducer(values)`, and delivers the reduced result to END.

    The worker node is registered as ``"worker"``; `MapNode`'s
    `worker_node` parameter is wired to ``"worker"`` automatically.

    Returns the uncompiled `Graph[S]` — call `.compile()` then pass to
    `GraphEngine` to execute.
    """
    g: Graph[S] = Graph()
    g.add_node("map", MapNode(items_fn=items_fn, worker_node="worker"))
    g.add_node("worker", worker_node)
    g.add_node("reduce", ReduceNode(reducer=reducer))
    g.add_edge(GraphNode.START, "map")
    g.add_edge("map", "worker")
    g.add_edge("worker", "reduce")
    g.add_edge("reduce", GraphNode.END)
    return g


__all__ = [
    "MapNode",
    "ReduceNode",
    "build_map_reduce_graph",
]
