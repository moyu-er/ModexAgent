"""`GraphEngine[S]` — thin entry point that delegates to a `Scheduler`.

Per the Scheduler ABC extraction: `GraphEngine` is now a delegator. It reads
`CompiledGraph.scheduler` (a `SchedulerKind`) at construction time, selects
the corresponding `Scheduler` implementation, and forwards `run_async` /
`run` calls to it.

Under the default `SchedulerKind.LINEAR`, `GraphEngine` delegates to
`LinearScheduler` — the verbatim extraction of the original sequential
execution logic. All existing graphs behave identically.

Under `SchedulerKind.PARALLEL`, `GraphEngine` delegates to
`ParallelScheduler` — the multi-instance execution model where nodes must
call `ctx.dispatch(target, state_update)` to route.

The original execution logic (run_async / run / _resolve_next / _execute_task)
now lives in `src/modex_graph/scheduler/linear.py` (`LinearScheduler`).

Per ADR-0033 D3 + D6 + D7 + D9.3 (unchanged):

- `run_async(ctx) -> S` — async entry. The primary mode for event-loop-bound
  agent runtimes (ReAct).
- `run(ctx) -> S` — sync entry. Wraps `run_async` in `asyncio.run`. For
  standalone scripts / CLI / REPL usage.
- `GraphBubbleUp` exceptions are NEVER swallowed — propagated to the caller.
- `max_iterations` safety net: exceeding raises `GraphRecursionError`.
- `run_async` is stateless across calls — always starts from `entry_node`.

Returns `ctx.state` (the final state). The terminal node writes its result
to a state field (e.g. `ctx.state.result = ...`); the caller reads it after
`run_async` returns. Per D9.3.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from typing_extensions import TypeVar

from .constants import SchedulerKind
from .scheduler import LinearScheduler, ParallelScheduler, Scheduler

if TYPE_CHECKING:
    from .compiled_graph import CompiledGraph
    from .context import GraphContext
    from .state import GraphState

S = TypeVar("S", bound="GraphState")


class GraphEngine[S: "GraphState"]:
    """Thin entry point that delegates execution to a `Scheduler`.

    Agnostic to ReAct / Hook / Interceptor / Approval. The engine's only
    concerns are: selecting the right `Scheduler` for the `CompiledGraph`'s
    `SchedulerKind`, and forwarding `run_async` / `run` calls to it.

    `GraphBubbleUp` exceptions propagate verbatim — the engine NEVER catches
    and swallows them. This is the formal "never swallow" rule from D7.

    Construction: `GraphEngine(compiled_graph)`. The scheduler is selected
    internally from `compiled_graph.scheduler`. Existing call sites
    (`GraphEngine(compiled)`) are unchanged.
    """

    def __init__(self, graph: CompiledGraph[S]) -> None:
        self.graph = graph
        self._scheduler: Scheduler[S] = self._select_scheduler(graph)

    @staticmethod
    def _select_scheduler(graph: CompiledGraph[S]) -> Scheduler[S]:
        """Select the `Scheduler` implementation for `graph.scheduler`.

        - `SchedulerKind.LINEAR` → `LinearScheduler` (sequential execution).
        - `SchedulerKind.PARALLEL` → `ParallelScheduler` (multi-instance
          execution with `ctx.dispatch` routing).
        """
        if graph.scheduler == SchedulerKind.LINEAR:
            return LinearScheduler(graph)
        if graph.scheduler == SchedulerKind.PARALLEL:
            return ParallelScheduler(graph)
        raise ValueError(
            f"Unknown scheduler kind {graph.scheduler!r}. "
            f"Supported: {SchedulerKind.LINEAR!r}, {SchedulerKind.PARALLEL!r}."
        )

    async def run_async(self, ctx: GraphContext[S]) -> S:
        """Run the graph from `entry_node` until `GraphNode.END`.

        Delegates to the selected `Scheduler`. Returns `ctx.state` (the
        final state). The terminal node writes its result to a state field;
        the caller reads it after this returns.

        `GraphBubbleUp` exceptions propagate to the caller. The engine does
        NOT catch them.
        """
        return await self._scheduler.run_async(ctx)

    def run(self, ctx: GraphContext[S]) -> S:
        """Sync entry. Delegates to the selected `Scheduler`.

        For standalone scripts / CLI / REPL usage. Event-loop-bound agent
        runtimes (ReAct) use `run_async` directly.
        """
        return self._scheduler.run(ctx)


__all__ = ["GraphEngine"]
