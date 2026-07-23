"""`GraphEngine[S]` — drives node execution + edge routing.

Per ADR-0033 D3 + D6 + D7 + D9.3:

- `run_async(ctx) -> S` — async entry. The primary mode for event-loop-bound
  agent runtimes (ReAct).
- `run(ctx) -> S` — sync entry. Wraps `run_async` in `asyncio.run`. For
  standalone scripts / CLI / REPL usage.
- Unifies sync/async node implementations via `inspect.isawaitable`.
- Four routing mechanisms, strict priority:
    1. `Command.goto` (if present) — dynamic routing / fan-out.
    2. `transition` (if present) — static edge lookup.
    3. Conditional edge (`route_fn(state)`) — if defined for current node.
    4. Default edge (`reason=None`) — fallback.
    5. Else raise `RoutingError`.
- `GraphBubbleUp` exceptions are NEVER swallowed — propagated to the caller.
- `max_iterations` safety net: exceeding raises `GraphRecursionError`.
- `run_async` is stateless across calls — always starts from `entry_node`.
  Resume logic is carried by graph topology (e.g. ReAct's StartNode detects
  suspended state and routes to TOOL).

Returns `ctx.state` (the final state). The terminal node writes its result
to a state field (e.g. `ctx.state.result = ...`); the caller reads it after
`run_async` returns. Per D9.3.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import TYPE_CHECKING, cast

from typing_extensions import TypeVar

from .constants import GraphNode
from .exceptions import GraphRecursionError, RoutingError
from .result import NodeResult, Task

if TYPE_CHECKING:
    from .compiled_graph import CompiledGraph
    from .context import GraphContext
    from .state import GraphState

S = TypeVar("S", bound="GraphState")


class GraphEngine[S: "GraphState"]:
    """Executes a `CompiledGraph` by iterating nodes and following edges.

    Agnostic to ReAct / Hook / Interceptor / Approval. The engine's only
    concerns are: node execution (sync/async unified), state_update
    application, routing resolution, and `max_iterations` safety net.

    `GraphBubbleUp` exceptions propagate verbatim — the engine NEVER catches
    and swallows them. This is the formal "never swallow" rule from D7.
    """

    def __init__(self, graph: CompiledGraph[S]) -> None:
        self.graph = graph

    async def run_async(self, ctx: GraphContext[S]) -> S:
        """Run the graph from `entry_node` until `GraphNode.END`.

        Returns `ctx.state` (the final state). The terminal node writes its
        result to a state field; the caller reads it after this returns.

        Re-entry semantics: always starts from `entry_node`. The engine
        is stateless across `run_async` calls — no internal "resume
        context". Resume routing is driven by `state.resume_target`
        (set by `ctx.interrupt(value, resume_to=...)`): the entry node
        reads it and routes via `Command(goto=...)`.

        `GraphBubbleUp` exceptions propagate to the caller. The engine does
        NOT catch them.
        """
        current: str = self.graph.entry_node
        iteration = 0
        # Pending queue for `Command(goto=list[str])` sequential multi-target.
        # When non-empty, the next node is popped from here, overriding
        # normal routing — UNLESS the current node returns its own Command.
        pending: list[str] = []

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

            # Resolve next node (strict priority, D6).
            # _resolve_next is async because list[Task] fan-out awaits node execution.
            current = await self._resolve_next(ctx, current, result, pending)
            iteration += 1

        return ctx.state

    def run(self, ctx: GraphContext[S]) -> S:
        """Sync entry. Wraps `run_async` in `asyncio.run`.

        For standalone scripts / CLI / REPL usage. Event-loop-bound agent
        runtimes (ReAct) use `run_async` directly.

        If called from within a running event loop (e.g. pytest-asyncio auto
        mode), runs the coroutine in a separate thread with its own loop to
        avoid `asyncio.run() cannot be called from a running event loop`.
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.run_async(ctx))
        # There's a running loop — run in a separate thread.
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(asyncio.run, self.run_async(ctx))
            return future.result()

    async def _resolve_next(
        self,
        ctx: GraphContext[S],
        current: str,
        result: NodeResult,
        pending: list[str],
    ) -> str:
        """Resolve the next node per the four-mechanism strict priority.

        Priority (D6):
        1. `result.command.goto` (if present) — dynamic routing / fan-out.
        2. `pending` queue (from a previous `Command(goto=list[str])`).
        3. `result.transition` — static edge lookup.
        4. Conditional edge (`route_fn(state)`) — if defined for `current`.
        5. Default edge (`reason=None`) — fallback.
        6. Else raise `RoutingError`.

        For `Command(goto=list[str])`: sets `pending` to the remaining targets
        (after the first) and returns the first target.

        For `Command(goto=list[Task])`: executes all tasks sequentially with
        independent state (via `ctx.fork`), merges state_updates back to the
        parent, then returns `GraphNode.END` (Phase a; Phase c adds proper
        fan-out → fan-in).

        For `Command(goto=str)`: returns the target directly.

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
                # Narrow the list element type by checking the first element.
                # Command.goto is typed as `str | list[str] | list[Task] | None`,
                # so a list is either all-str or all-Task by construction
                # (validated at the Pydantic level). We check the first element
                # to narrow for the type checker.
                first_elem = goto[0]
                if isinstance(first_elem, str):
                    # list[str] — sequential multi-target.
                    # First target is next; rest go into pending.
                    str_goto = cast(list[str], goto)
                    first = str_goto[0]
                    rest = str_goto[1:]
                    pending.clear()
                    pending.extend(rest)
                    return first
                if isinstance(first_elem, Task):
                    # list[Task] — sequential fan-out with independent state.
                    # Execute all tasks inline, merge state_updates to parent,
                    # then go to END (Phase a). Phase c will add proper
                    # fan-out → fan-in with reducer channels.
                    task_goto = cast(list[Task], goto)
                    for task in task_goto:
                        await self._execute_task(ctx, task)
                    pending.clear()
                    return GraphNode.END
                # Mixed or unknown element type — invalid.
                raise RoutingError(
                    f"Command.goto list must be all str or all Task, got "
                    f"first element of type {type(first_elem).__name__}"
                )
            # Unknown goto type — invalid.
            raise RoutingError(
                f"Command.goto must be str | list[str] | list[Task] | None, "
                f"got {type(goto).__name__}"
            )

        # 2. Pending queue (from a previous list[str]).
        if pending:
            return pending.pop(0)

        # 3. transition — static edge lookup.
        if result.transition is not None:
            target = self.graph.next_node_by_transition(current, result.transition)
            if target is not None:
                return target
            # No matching static edge — fall through to conditional/default.
            # This is intentional: transition may not have a matching edge,
            # and the graph may have a default edge to handle it.

        # 4. Conditional edge.
        cond = self.graph.conditional_for(current)
        if cond is not None:
            route_key = cond.route_fn(ctx.state)
            if cond.destinations is not None:
                # Key-mapped mode: route_fn returns a key, mapped to a node.
                target = cond.destinations.get(route_key)
                if target is not None:
                    return target
                # Unknown key — fall through to default edge.
            else:
                # Direct mode: route_fn return value IS the node name.
                return route_key

        # 5. Default edge (reason=None).
        default_target = self.graph.default_edge_target(current)
        if default_target is not None:
            return default_target

        # 6. No routing match — raise.
        raise RoutingError(
            f"No routing match from node {current!r}: "
            f"transition={result.transition!r}, "
            f"command={result.command!r}, "
            f"conditional_edge={'yes' if cond is not None else 'no'}, "
            f"default_edge={'yes' if default_target is not None else 'no'}"
        )

    async def _execute_task(self, parent_ctx: GraphContext[S], task: Task) -> None:
        """Execute a single fan-out `Task` inline (Phase-a sequential).

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


__all__ = ["GraphEngine"]
