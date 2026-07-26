"""``LinearScheduler[S]`` — sequential execution strategy.

Verbatim extraction of the original ``GraphEngine`` sequential execution
logic. Under the default ``SchedulerKind.LINEAR``, all existing graphs behave
identically to before the extraction — zero behaviour change.

``GraphBubbleUp`` exceptions propagate verbatim — the scheduler NEVER catches
and swallows them (D7).
"""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, cast

from ..constants import GraphNode
from ..exceptions import GraphRecursionError, RoutingError
from ..result import NodeResult, Task
from .base import Scheduler

if TYPE_CHECKING:
    from ..compiled_graph import CompiledGraph
    from ..context import GraphContext
    from ..state import GraphState


class LinearScheduler[S: "GraphState"](Scheduler[S]):
    """Sequential scheduler — executes nodes one at a time following edges.

    This is the verbatim extraction of the original `GraphEngine` execution
    logic. All existing graphs behave identically under this scheduler.

    Owns:

    - `run_async` — async entry; iterates nodes from `entry_node` to
      `GraphNode.END`, applying state updates and resolving the next node.
    - `_resolve_next` — two-layer strict-priority routing resolution.
    - `_execute_task` — sequential fan-out task execution with independent
      state per `Task`.

    `run` (sync entry) is inherited from the `Scheduler` ABC.

    `GraphBubbleUp` exceptions propagate verbatim — the scheduler NEVER
    catches and swallows them (D7).
    """

    def __init__(self, graph: CompiledGraph[S]) -> None:
        self.graph = graph

    async def run_async(self, ctx: GraphContext[S]) -> S:
        """Run the graph from `entry_node` until `GraphNode.END`.

        Returns `ctx.state` (the final state). The terminal node writes its
        result to a state field; the caller reads it after this returns.

        Re-entry semantics: always starts from `entry_node`. The scheduler
        is stateless across `run_async` calls — no internal "resume
        context". Resume routing is driven by `state.resume_target`
        (set by `ctx.interrupt(value, resume_to=...)`): the entry node
        reads it and routes via `Command(goto=...)`.

        `GraphBubbleUp` exceptions propagate to the caller. The scheduler does
        NOT catch them.
        """
        current: str = self.graph.entry_node
        iteration = 0

        while current != GraphNode.END:
            # Engine-level safety net (D9.3 layer 1).
            if iteration >= self.graph.max_iterations:
                raise GraphRecursionError(
                    f"Graph exceeded max_iterations={self.graph.max_iterations} "
                    f"(last node: {current!r}). This is an abnormal exit — "
                    f"the business-level max iteration count should route to "
                    f"END via a static edge before this safety net fires."
                )

            node = self.graph.nodes[current]

            # Engine-auto-invoked lifecycle hook (D5: before_node).
            await ctx.runtime.before_node(ctx, current)

            # Execute the node. Sync/async unified via inspect.isawaitable.
            # GraphBubbleUp exceptions propagate — NOT caught here.
            raw_result = node.execute(ctx)
            if inspect.isawaitable(raw_result):
                result: NodeResult = await raw_result
            else:
                result = raw_result

            # Apply declarative state_update (D4 Z-style declarative mode).
            if result.state_update is not None:
                ctx.state.apply_state_update(result.state_update)

            # Engine-auto-invoked lifecycle hook (D5: after_node).
            await ctx.runtime.after_node(ctx, current, result)

            # Resolve next node (strict priority, D12).
            # _resolve_next is async because list[Task] fan-out awaits node execution.
            current = await self._resolve_next(ctx, current, result)
            iteration += 1

        return ctx.state

    async def _resolve_next(
        self,
        ctx: GraphContext[S],
        current: str,
        result: NodeResult,
    ) -> str:
        """Resolve the next node per the two-layer routing model.

        Priority (ADR-0034 D12):
        1. Dynamic layer — `result.command.goto` (if present): routing /
           fan-out.
        2. Static layer — `result.transition` matched against static edges.
        3. Default edge (`reason=None`) — fallback.
        4. Else raise `RoutingError`.

        For `Command(goto=str)`: returns the target directly.

        For `Command(goto=list[Task])`: executes all tasks sequentially with
        independent state (via `ctx.fork`), merges state_updates back to the
        parent, then returns `GraphNode.END` (`LinearScheduler` sequential
        fan-out; `ParallelScheduler` adds proper fan-out → fan-in, ADR-0034).

        This method is `async` because `list[Task]` fan-out awaits node
        execution inline.
        """
        # 1. Command.goto (highest priority).
        if result.command is not None and result.command.goto is not None:
            goto = result.command.goto
            if isinstance(goto, str):
                # Single str — dynamic routing to one node.
                return goto
            if isinstance(goto, list):
                if len(goto) == 0:
                    # Empty list — treat as "go to END".
                    return GraphNode.END
                # Command.goto is typed as `str | list[Task] | None`, so a
                # non-empty list is all-Task by construction (validated at
                # the Pydantic level via the _reject_str_list validator).
                first_elem = goto[0]
                if isinstance(first_elem, Task):
                    for task in goto:
                        await self._execute_task(ctx, task)
                    return GraphNode.END
                # Unknown element type — invalid (should be unreachable due
                # to Pydantic validation, but guard defensively).
                raise RoutingError(
                    f"Command.goto list must contain Task instances, got "
                    f"first element of type {type(first_elem).__name__}"
                )
            # Unknown goto type — invalid.
            raise RoutingError(
                f"Command.goto must be str | list[Task] | None, got {type(goto).__name__}"
            )

        # 2. transition — static edge lookup.
        if result.transition is not None:
            target = self.graph.next_node_by_transition(current, result.transition)
            if target is not None:
                return target
            # No matching static edge — fall through to default.
            # This is intentional: transition may not have a matching edge,
            # and the graph may have a default edge to handle it.

        # 3. Default edge (reason=None).
        default_target = self.graph.default_edge_target(current)
        if default_target is not None:
            return default_target

        # 4. No routing match — raise.
        raise RoutingError(
            f"No routing match from node {current!r}: "
            f"transition={result.transition!r}, "
            f"command={result.command!r}, "
            f"default_edge={'yes' if default_target is not None else 'no'}"
        )

    async def _execute_task(self, parent_ctx: GraphContext[S], task: Task) -> None:
        """Execute a single fan-out `Task` inline (sequential, LinearScheduler).

        Forks the context with the task's state (independent if provided),
        runs the task's node, and merges `NodeResult.state_update` back to
        the PARENT ctx.state via reducer channels.

        Per D5.2: imperative mutations on the forked state do NOT propagate
        to the parent. Only `NodeResult.state_update` merges back.

        GraphBubbleUp exceptions propagate (not caught here).
        """
        # Fork the context. If task.state is None, share parent state.
        # task.state is `Any | None`; ctx.fork expects `S | None`. The cast
        # is safe because task.state is either None (share parent) or an
        # instance of S (the caller is responsible for type correctness).
        sub_state = cast("S | None", task.state)
        sub_ctx = parent_ctx.fork(state=sub_state)
        node = self.graph.nodes[task.node]

        # Engine-auto-invoked lifecycle hooks for the task node.
        await sub_ctx.runtime.before_node(sub_ctx, task.node)

        # Execute the task node. Sync/async unified.
        raw_result = node.execute(sub_ctx)
        if inspect.isawaitable(raw_result):
            result: NodeResult = await raw_result
        else:
            result = raw_result

        # Merge state_update back to the PARENT ctx.state (not sub_ctx).
        # Per D5.2: only state_update merges; imperative mutations don't.
        if result.state_update is not None:
            parent_ctx.state.apply_state_update(result.state_update)

        await sub_ctx.runtime.after_node(sub_ctx, task.node, result)


__all__ = ["LinearScheduler"]
