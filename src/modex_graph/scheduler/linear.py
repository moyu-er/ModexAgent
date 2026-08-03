"""``LinearScheduler[S]`` — sequential execution strategy.

Deliver-only routing: nodes MUST call ``deliver()``
during ``execute()``. The scheduler reads the recorded dispatches (via
``ctx.dispatch`` → ``_handle_linear_dispatch``) for the next target.
Upstream payloads now flow through the coordinator's
``collect_consumable_delivers``; the dispatch handler →
``coordinator.route_deliver`` wiring is in the scheduler.

``GraphBubbleUp`` exceptions propagate verbatim — the scheduler NEVER catches
and swallows them (D7).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..constants import GraphNode, SchedulerKind
from ..exceptions import GraphRecursionError, RoutingError
from .base import Scheduler

if TYPE_CHECKING:
    from ..compiled_graph import CompiledGraph
    from ..context import GraphContext
    from ..state import GraphState


class LinearScheduler[S: "GraphState"](Scheduler[S]):
    """Sequential scheduler — executes nodes one at a time following edges.

    Deliver-only routing: reads recorded dispatches (via ``ctx.dispatch``)
    for the next target. Upstream payloads flow through the coordinator.

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
        # Stored ctx for dispatch handler access to coordinator.
        self._ctx: GraphContext[S] | None = None

    async def run_async(self, ctx: GraphContext[S]) -> S:
        """Run the graph from `entry_node` until `GraphNode.END`.

        Returns `ctx.state` (the final state). The terminal node writes its
        result to a state field; the caller reads it after this returns.

        Re-entry semantics: always starts from `entry_node`. The scheduler
        is stateless across `run_async` calls — no internal "resume
        context". Resume routing is driven by `state.resume_target`
        (set by `ctx.interrupt(value, resume_to=...)`): the entry node
        reads it and routes via `deliver()`.

        Routing is deliver-only: nodes MUST call
        ``deliver()`` during ``execute()``. The ``_submit`` step calls
        ``ctx.dispatch(target, ...)`` for each deliver group; the linear
        dispatch handler records the target + payload. The scheduler reads
        the first recorded target as the next node (LINEAR is sequential).
        Upstream payloads flow through the coordinator.

        `GraphBubbleUp` exceptions propagate to the caller. The scheduler does
        NOT catch them.
        """
        ctx.scheduler_kind = SchedulerKind.LINEAR
        ctx.set_dispatch_handler(self._handle_linear_dispatch)
        self._ctx = ctx

        current: str = self.graph.entry_node
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

            # Execute via run() — pass graph topology so _resolve_default_target
            # can resolve next_node=None. Upstream payloads flow through
            # coordinator.collect_consumable_delivers. The dispatch
            # handler calls coordinator.route_deliver to route delivers to
            # the target node's deliver_store.
            result = await node.run(
                ctx,
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
                raise RoutingError(f"Node {previous!r} did not deliver.")

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
        ``run_async`` can determine the next node. Also routes the deliver
        to the target node's deliver_store via the coordinator.
        """
        payload = state_update.get("delivered") if state_update else None
        self._dispatches.setdefault(target, []).append(payload)
        if self._ctx is not None and target != GraphNode.END:
            source_node = (
                state_update.get("_source_node", source_instance)
                if state_update
                else source_instance
            )
            source_inv_id = state_update.get("_source_inv_id", 0) if state_update else 0
            self._ctx.coordinator.route_deliver(target, payload, source_node, source_inv_id)


__all__ = ["LinearScheduler"]
