"""``LinearScheduler[S]`` — sequential execution strategy.

Deliver-only routing (P3.4b convergence): nodes MUST call ``deliver()``
during ``execute()``. The scheduler reads the recorded dispatches (via
``ctx.dispatch`` → ``_handle_linear_dispatch``) for the next target and
passes the dispatch payloads as ``upstream_payloads`` to the downstream
node's ``run()``. ``Command``/``Task``/``transition`` routing were removed
as dead code.

``GraphBubbleUp`` exceptions propagate verbatim — the scheduler NEVER catches
and swallows them (D7).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..constants import GraphNode, SchedulerKind
from ..exceptions import GraphRecursionError, RoutingError
from ..integration import IntegratedPayload
from .base import Scheduler

if TYPE_CHECKING:
    from ..compiled_graph import CompiledGraph
    from ..context import GraphContext
    from ..state import GraphState


class LinearScheduler[S: "GraphState"](Scheduler[S]):
    """Sequential scheduler — executes nodes one at a time following edges.

    Deliver-only routing: reads recorded dispatches (via ``ctx.dispatch``)
    for the next target, passes dispatch payloads as ``upstream_payloads``
    to the downstream node.

    Owns:

    - `run_async` — async entry; iterates nodes from `entry_node` to
      `GraphNode.END`, applying state updates, resolving the next node
      via recorded dispatches, and passing upstream payloads.

    `run` (sync entry) is inherited from the `Scheduler` ABC.

    `GraphBubbleUp` exceptions propagate verbatim — the scheduler NEVER
    catches and swallows them (D7).
    """

    def __init__(self, graph: CompiledGraph[S]) -> None:
        self.graph = graph
        # Per-node dispatch records: target → [payloads]. Reset at the top
        # of each loop iteration. Populated by `_handle_linear_dispatch`.
        self._dispatches: dict[str, list[Any]] = {}

    async def run_async(self, ctx: GraphContext[S]) -> S:
        """Run the graph from `entry_node` until `GraphNode.END`.

        Returns `ctx.state` (the final state). The terminal node writes its
        result to a state field; the caller reads it after this returns.

        Re-entry semantics: always starts from `entry_node`. The scheduler
        is stateless across `run_async` calls — no internal "resume
        context". Resume routing is driven by `state.resume_target`
        (set by `ctx.interrupt(value, resume_to=...)`): the entry node
        reads it and routes via `deliver()`.

        Routing is deliver-only (P3.4b convergence): nodes MUST call
        ``deliver()`` during ``execute()``. The ``_submit`` step calls
        ``ctx.dispatch(target, ...)`` for each deliver group; the linear
        dispatch handler records the target + payload. The scheduler reads
        the first recorded target as the next node (LINEAR is sequential)
        and passes the payloads as ``upstream_payloads`` to the downstream
        node's ``run()``.

        `GraphBubbleUp` exceptions propagate to the caller. The scheduler does
        NOT catch them.
        """
        ctx.scheduler_kind = SchedulerKind.LINEAR
        ctx.set_dispatch_handler(self._handle_linear_dispatch)

        current: str = self.graph.entry_node
        upstream_payloads: list[IntegratedPayload] | None = None
        iteration = 0

        while current != GraphNode.END:
            if iteration >= self.graph.max_iterations:
                raise GraphRecursionError(
                    f"Graph exceeded max_iterations={self.graph.max_iterations} "
                    f"(last node: {current!r}). This is an abnormal exit — "
                    f"the business-level max iteration count should route to "
                    f"END via a static edge before this safety net fires."
                )

            # Reset per-node dispatch records before executing.
            self._dispatches = {}
            ctx.set_current_instance(current)

            node = self.graph.nodes[current]

            await ctx.runtime.before_node(ctx, current)

            # Execute via run() — enforce_deliver=True, pass graph topology
            # so _resolve_default_target can resolve next_node=None. Pass
            # upstream_payloads from the previous node's dispatch.
            result = await node.run(
                ctx,
                upstream_payloads=upstream_payloads,
                enforce_deliver=True,
                graph=self.graph,
            )

            if result.state_update is not None:
                ctx.state.apply_state_update(result.state_update)

            await ctx.runtime.after_node(ctx, current, result)

            # Deliver-only routing: read recorded dispatches for next target.
            # LINEAR is sequential — take the first target.
            previous = current
            if self._dispatches:
                current = next(iter(self._dispatches.keys()))
            elif node.result:
                # Fallback: read _submit_result (LINEAR-only safety net
                # for custom submit overrides that don't call _submit).
                current = next(iter(node.result.keys()))
            else:
                raise RoutingError(
                    f"Node {previous!r} did not deliver."
                )

            # Prepare upstream_payloads for the next node from the dispatch
            # payloads recorded for the selected target.
            if current != GraphNode.END:
                upstream_payloads = [
                    IntegratedPayload(source_node=previous, content=p)
                    for p in self._dispatches.get(current, [])
                ]
            else:
                upstream_payloads = None

            iteration += 1

        ctx.set_current_instance(None)
        return ctx.state

    def _handle_linear_dispatch(
        self,
        source_instance: str,
        target: str,
        state_update: dict[str, Any] | None,
    ) -> None:
        """Record a dispatch for LINEAR next-node selection.

        Called synchronously from ``GraphContext.dispatch`` during
        ``Node._submit``. Records the target and extracted payload so
        ``run_async`` can determine the next node and build
        ``upstream_payloads`` for it.
        """
        payload = state_update.get("delivered") if state_update else None
        self._dispatches.setdefault(target, []).append(payload)


__all__ = ["LinearScheduler"]
