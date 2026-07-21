"""MapReduce graph pattern over `modex_graph`.

Reusable split -> fan-out -> reduce workflow composing the retained
`modex_graph` API:

- `Command(goto=list[Task])` for fan-out (Phase-a sequential; Phase-c
  parallel via `asyncio.gather` — ADR-0033 D12).
- `Task(state=...)` for independent per-worker state.
- `ReducerChannel` for fan-in (folds all workers' `state_update`
  contributions).
- `ctx.fork(state=None)` for the reduce task to share parent state.

This is example code (lives under `examples/` per ADR-0007 rule 9). It
uses only the public `modex_graph` API — no framework-internal hooks, no
`modex_agent` imports. The pattern is generic over any `GraphState`
subclass.

Topology (Phase-a execution shape)::

    START -> map -> [worker_1, ..., worker_n, reduce] -> END

The map node emits `Command(goto=list[Task])` with one `Task` per item
returned by `items_fn(ctx.state)`. Each worker task carries an
independent state (constructed by `state_fn`) so imperative mutations in
one worker do not propagate to siblings — only `NodeResult.state_update`
merges back to the parent via `ReducerChannel`.

**Phase-a engine note:** `GraphEngine._resolve_next` routes to
`GraphNode.END` immediately after `list[Task]` completes — there is no
edge-based continuation from the node that returned the `Command`. To run
`ReduceNode` after the workers, `build_map_reduce_graph` wraps `MapNode`
in an internal `_MapWithReduceNode` that appends
`Task(node="reduce", state=None)` to the fan-out list. `state=None` makes
the reduce task share the parent state (`ctx.fork(state=None)` returns a
sub-context with the same state object), so `ReduceNode`'s imperative
`setattr(ctx.state, result_field, reduced)` writes the reduced result
directly to the final parent state.

**Phase-c upgrade path:** when the engine adds parallel fan-out + edge-
based fan-in continuation (ADR-0033 D12), the declaration shape
(`Command(goto=list[Task])` + `ReducerChannel`) is identical and upgrades
automatically. The reduce task would then run via a normal edge from the
map node instead of being appended to the fan-out list; `MapNode` and
`ReduceNode` themselves are unchanged.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

from modex_graph import (
    Command,
    Graph,
    GraphContext,
    GraphNode,
    GraphState,
    Node,
    NodeResult,
    Task,
)


class MapNode[S: GraphState](Node[S]):
    """Fan-out node: emits `Command(goto=list[Task])` with one Task per item.

    `execute` reads `items_fn(ctx.state)` to get a list of items, then
    returns `NodeResult(command=Command(goto=[Task(node=worker_node,
    state=state_fn(item)) for item in items]))`.

    Each `Task` carries an independent state (constructed by `state_fn`)
    so imperative mutations in one worker do not propagate to siblings.
    Only `NodeResult.state_update` merges back to the parent via
    `ReducerChannel`.

    Example::

        g.add_node("map", MapNode(
            items_fn=lambda s: s.items,
            worker_node="worker",
            state_fn=lambda item: MyState(current_item=item),
        ))
    """

    def __init__(
        self,
        items_fn: Callable[[S], list[Any]],
        worker_node: str,
        state_fn: Callable[[Any], S],
    ) -> None:
        self.items_fn = items_fn
        self.worker_node = worker_node
        self.state_fn = state_fn

    def execute(self, ctx: GraphContext[S]) -> NodeResult:
        items = self.items_fn(ctx.state)
        tasks = [Task(node=self.worker_node, state=self.state_fn(item)) for item in items]
        return NodeResult(command=Command(goto=tasks))


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
    subclass and any field names, exactly like `_RetryBodyWrapper` in
    `retry.py` uses `getattr`/`setattr` for its counter field.

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

    def execute(self, ctx: GraphContext[S]) -> NodeResult:
        values = getattr(ctx.state, self.source_field)
        reduced = self.reducer(values)
        setattr(ctx.state, self.result_field, reduced)
        return NodeResult()


class _MapWithReduceNode[S: GraphState](Node[S]):
    """Internal wrapper: fan-out workers + append a reduce task.

    Used by `build_map_reduce_graph`. Not part of the public API.

    Phase-a engine semantics route to `GraphNode.END` immediately after
    `list[Task]` completes (no edge-based continuation from the
    originating node — see `GraphEngine._resolve_next`). To run
    `ReduceNode` after the workers, this wrapper appends
    `Task(node=reduce_node, state=None)` to the `MapNode`'s task list.
    `state=None` makes the reduce task share the parent state
    (`ctx.fork(state=None)` returns a sub-context with the same state
    object), so `ReduceNode`'s imperative `setattr` writes the reduced
    result directly to the final parent state.

    `MapNode` itself stays pure (emits only worker tasks, matching the
    spec exactly); the reduce-task append is an engine-compatibility
    concern isolated in this wrapper.
    """

    def __init__(self, map_node: MapNode[S], reduce_node: str) -> None:
        self.map_node = map_node
        self.reduce_node = reduce_node

    def execute(self, ctx: GraphContext[S]) -> NodeResult:
        map_result = self.map_node.execute(ctx)
        # MapNode always returns Command(goto=list[Task]). The asserts
        # narrow the Optional types for the type checker and document the
        # invariant; if MapNode ever changes shape, these fail loudly.
        command = map_result.command
        assert command is not None and command.goto is not None
        worker_tasks = cast(list[Task], command.goto)
        all_tasks = [*worker_tasks, Task(node=self.reduce_node, state=None)]
        return NodeResult(command=Command(goto=all_tasks))


def build_map_reduce_graph[S: GraphState](
    items_fn: Callable[[S], list[Any]],
    state_fn: Callable[[Any], S],
    worker_node: Node[S],
    reducer: Callable[[list[Any]], Any],
    source_field: str,
    result_field: str,
) -> Graph[S]:
    """Build a split -> fan-out -> reduce topology.

    Topology (Phase-a execution shape)::

        START -> map -> [worker_1, ..., worker_n, reduce] -> END

    - ``map`` is a `MapNode` wrapped by `_MapWithReduceNode` so the reduce
      task runs as the final fan-out task (Phase-a engine routes to `END`
      after `list[Task]`; the reduce task shares parent state via
      `Task(state=None)` and writes the result imperatively).
    - ``worker`` processes one item and returns
      `NodeResult(state_update={source_field: [processed_result]})`. Each
      worker task carries an independent state (via `state_fn`), so
      imperative mutations do not propagate to siblings.
    - ``reduce`` is a `ReduceNode` that reads the `ReducerChannel`-backed
      `source_field` (which has accumulated all workers' contributions),
      applies `reducer(values)`, and writes the result to `result_field`.

    The worker node is registered as ``"worker"`` and the reduce node as
    ``"reduce"``; `MapNode`'s `worker_node` parameter is wired to
    ``"worker"`` automatically.

    Returns the uncompiled `Graph[S]` — call `.compile()` then pass to
    `GraphEngine` to execute.

    Example::

        g = build_map_reduce_graph(
            items_fn=lambda s: s.items,
            state_fn=lambda item: MyState(current_item=item),
            worker_node=SquareWorker(),
            reducer=sum,
            source_field="squares",
            result_field="total",
        )
        compiled = g.compile()
        result = await GraphEngine(compiled).run_async(ctx)
    """
    g: Graph[S] = Graph()
    map_node = MapNode(items_fn=items_fn, worker_node="worker", state_fn=state_fn)
    g.add_node(
        "map",
        _MapWithReduceNode(map_node=map_node, reduce_node="reduce"),
    )
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
    return g


__all__ = [
    "MapNode",
    "ReduceNode",
    "build_map_reduce_graph",
]
