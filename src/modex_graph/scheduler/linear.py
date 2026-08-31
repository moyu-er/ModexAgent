"""``LinearScheduler[S]`` — sequential execution strategy.

Deliver-only routing: nodes MUST call ``deliver()``
during ``execute()``. The scheduler reads the recorded dispatches (via
``ctx.dispatch`` → ``_handle_linear_dispatch``) for the next target.
Upstream payloads now flow through the coordinator's
``collect_consumable_delivers``; dispatch carries only a scheduling wakeup.

``GraphBubbleUp`` exceptions propagate verbatim — the scheduler NEVER catches
and swallows them (D7).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..constants import GraphNode, SchedulerKind
from ..exceptions import GraphRecursionError
from ..execution_context import NodeExecution, reset_execution, set_execution
from ._dispatch_utils import record_end_reachability, validate_dispatch_target
from .base import Scheduler
from .bootstrap import BootstrapMode, bootstrap

if TYPE_CHECKING:
    from ..compiled_graph import CompiledGraph
    from ..context import GraphContext
    from ..state import GraphState


class LinearScheduler[S: "GraphState"](Scheduler[S]):
    """Sequential scheduler — executes nodes one at a time following edges.

    Deliver-only routing: reads recorded dispatches (via ``ctx.dispatch``)
    for the next target. Upstream payloads flow through the coordinator.

    Owns:

    - `run_async` — async entry; calls ``bootstrap`` to derive seed node
      names, takes the first as the start, then iterates to
      `GraphNode.END` via recorded dispatches.

    `run` (sync entry) is inherited from the `Scheduler` ABC.

    `GraphBubbleUp` exceptions propagate verbatim — the scheduler NEVER
    catches and swallows them (D7).
    """

    def __init__(self, graph: CompiledGraph[S]) -> None:
        self.graph = graph
        # Per-node dispatch records: target → [payloads]. Reset at the top
        # of each loop iteration. Populated by `_handle_linear_dispatch`.
        self._dispatches: dict[str, list[Any]] = {}
        # Stored ctx for dispatch handler access to coordinator.
        self._ctx: GraphContext[S] | None = None

    async def run_async(self, ctx: GraphContext[S], *, mode: BootstrapMode) -> S:
        """Run the graph from its derived start until `GraphNode.END`.

        Returns `ctx.state` (the final state). The terminal node writes its
        result to a state field; the caller reads it after this returns.

        ``bootstrap`` derives seed node names from persisted invocation
        status and PENDING delivers (BFS-ordered). The scheduler takes
        ``seeds[0]`` as the start, falling back to ``entry_node`` when no
        seeds are produced. HITL resume routing remains driven by
        `state.resume_target` (set by
        `ctx.interrupt(value, resume_to=...)`).

        Routing is deliver-only: nodes MUST call
        ``deliver()`` during ``execute()``. After node completion,
        ``Node.run`` calls ``ctx.dispatch(target)`` for each affected target;
        the linear dispatch handler records the target. The scheduler reads
        the first recorded target as the next node (LINEAR is sequential).
        Upstream payloads flow through the coordinator.

        `GraphBubbleUp` exceptions propagate to the caller. The scheduler does
        NOT catch them.
        """
        ctx.scheduler_kind = SchedulerKind.LINEAR
        ctx.set_dispatch_handler(self._handle_linear_dispatch)
        self._ctx = ctx

        seeds = bootstrap(ctx, self.graph, mode=mode)
        current = seeds[0] if seeds else self.graph.entry_node
        iteration = 0

        while True:
            ctx.control.check()
            if (
                self.graph.max_iterations is not None
                and iteration >= self.graph.max_iterations
            ):
                raise GraphRecursionError(
                    f"Graph exceeded max_iterations={self.graph.max_iterations} "
                    f"(last node: {current!r}). This is an abnormal exit — "
                    f"the business-level max iteration count should route to "
                    f"END via a static edge before this safety net fires."
                )

            # Reset per-node dispatch records before executing.
            self._dispatches = {}

            node = self.graph.nodes[current]

            node_exec = NodeExecution(instance_id=current)
            exec_token = set_execution(node_exec)

            try:
                await ctx.runtime.before_node(ctx, current)
                await node.run(ctx, graph=self.graph)
                await ctx.runtime.after_node(ctx, current)
            finally:
                reset_execution(exec_token)

            if current == GraphNode.END:
                break

            # Deliver-only routing: read recorded dispatches for next target.
            # LINEAR is sequential — take the first target.
            if self._dispatches:
                current = next(iter(self._dispatches.keys()))
            else:
                # Dead-end: node produced no dispatches → graph did not reach END.
                ctx.reached_end = False
                break

            iteration += 1

        return ctx.state

    def _handle_linear_dispatch(
        self,
        source_instance: str,
        target: str,
    ) -> None:
        """Record a dispatch for LINEAR next-node selection.

        Called synchronously from ``GraphContext.dispatch`` after a node's
        staged outputs are promoted. Records the target so ``run_async`` can
        determine the next node.

        Topology enforcement and END reachability are shared with
        ``ParallelScheduler._handle_dispatch`` via ``_dispatch_utils``.
        """
        validate_dispatch_target(self.graph, source_instance, target)

        self._dispatches.setdefault(target, [])
        assert self._ctx is not None
        record_end_reachability(self._ctx, target)


__all__ = ["LinearScheduler"]
