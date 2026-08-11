"""Retry graph patterns over `modex_graph`.

Two forms of retry, exercising different parts of the deliver/submit API:

1. ``RetryNode[S]`` — retry within a single ``execute`` call.
   The node wraps a body ``Node`` and awaits ``body.execute(ctx)`` up to
   ``max_retries + 1`` times. If ``is_failure(ctx.state)`` returns ``False``,
   the node delivers to its default target (success). If all attempts fail,
   the node delivers to ``failure_target`` (exhaustion). This form exercises
   ``Node`` composition without involving graph topology for the retry loop.

2. ``build_retry_graph(...)`` — topology retry via self-loop. The body node
   is wired to itself (self-loop edge) and to ``GraphNode.END``. A counter
   on the wrapper instance tracks attempt count; when the counter reaches
   ``max_retries``, the wrapper delivers to END instead of back to itself.
   This form exercises self-loop edges and ``max_iterations`` as a panic
   safety net (set to ``max_retries + 5``).

The ``is_failure`` callback takes the state ``S`` and returns ``True`` if
the body failed. The body signals failure via imperative state mutation
(e.g. ``ctx.state.exit_path = "fail"``). The former ``transition``-based
signaling was removed (P3.4b convergence).

The body reports outcomes through imperative state mutation.

This is example code (lives under ``examples/`` per ADR-0007 rule 9). It
uses only the public ``modex_graph`` API — no framework-internal hooks, no
``modex_agent`` imports. The patterns are generic over any ``GraphState``
subclass.
"""

from __future__ import annotations

from collections.abc import Callable

from modex_graph import (
    CompiledGraph,
    Graph,
    GraphContext,
    GraphNode,
    GraphState,
    IntegratedInput,
    Node,
)


class RetryNode[S: GraphState](Node[S]):
    """Retry within a single ``execute`` call.

    Calls ``body.execute(ctx)`` up to ``max_retries + 1`` times.
    If ``is_failure(ctx.state)`` returns ``False``, the node delivers to
    ``success_target``. If all attempts fail, the node delivers to
    ``failure_target``.

    The body signals failure via imperative state mutation (e.g.
    ``ctx.state.exit_path = "fail"``). ``is_failure`` reads the state to
    determine if the body failed.

    Example::

        g.add_node("retry", RetryNode(body, max_retries=3, is_failure=lambda s: s.exit_path == "fail", success_target="default_exit", failure_target="failed_exit"))
        g.add_edge(GraphNode.START, "retry")
        g.add_edge("retry", "failed_exit")
        g.add_edge("retry", "default_exit")
    """

    def __init__(
        self,
        body: Node[S],
        max_retries: int,
        is_failure: Callable[[S], bool],
        success_target: str,
        failure_target: str,
    ) -> None:
        self.body = body
        self.max_retries = max_retries
        self.is_failure = is_failure
        self.success_target = success_target
        self.failure_target = failure_target

    async def execute(self, ctx: GraphContext[S], integrated_input: IntegratedInput) -> None:
        for _ in range(self.max_retries + 1):
            await self.body.execute(ctx, integrated_input)
            if not self.is_failure(ctx.state):
                self.deliver(None, self.success_target, ctx)
                return None
        self.deliver(None, self.failure_target, ctx)
        return None


class _RetryBodyWrapper[S: GraphState](Node[S]):
    """Internal retry-aware wrapper around a user-provided body node.

    On each execution:
    1. Awaits ``body.execute(ctx)``.
    2. Increments the attempt counter (stored in ``node_scratch``).
    3. If ``not is_failure(ctx.state)``: delivers to ``GraphNode.END``.
    4. If ``is_failure(ctx.state)`` and counter < ``max_retries``: delivers
       to ``"body"`` (self-loop).
    5. If ``is_failure(ctx.state)`` and counter >= ``max_retries``: delivers
       to ``GraphNode.END`` (exhaustion).

    The counter lives in ``ctx.state.node_scratch[self.node_id]`` —
    graph-run-scoped (persists across self-loop invocations within one
    run, resets to 0 on the next run because each run gets a fresh
    ``ctx.state``). Safe under the per-node serial gate (same ``Node``
    object never executes concurrently under either scheduler).
    """

    def __init__(
        self,
        body: Node[S],
        max_retries: int,
        is_failure: Callable[[S], bool],
    ) -> None:
        self.body = body
        self.max_retries = max_retries
        self.is_failure = is_failure

    async def execute(self, ctx: GraphContext[S], integrated_input: IntegratedInput) -> None:
        scratch = ctx.state.node_scratch
        attempt = scratch.get(self.node_id, 0)
        await self.body.execute(ctx, integrated_input)
        attempt += 1
        scratch[self.node_id] = attempt
        if not self.is_failure(ctx.state):
            self.deliver(None, GraphNode.END, ctx)
            return None
        if attempt < self.max_retries:
            self.deliver(None, "body", ctx)
            return None
        self.deliver(None, GraphNode.END, ctx)
        return None


def build_retry_graph[S: GraphState](
    body_node: Node[S],
    max_retries: int,
    is_failure: Callable[[S], bool],
) -> CompiledGraph[S]:
    """Build a retry-via-self-loop topology and compile it.

    Topology::

        START -> body -> body (self-loop)
                   -> END

    The user-provided ``body_node`` is wrapped in a ``_RetryBodyWrapper``
    that manages the attempt counter (instance-local) and decides whether
    to self-loop or exit.

    Returns the compiled graph — pass to ``GraphEngine`` to execute.

    Example::

        compiled = build_retry_graph(
            body_node=MyBodyNode(),
            max_retries=3,
            is_failure=lambda s: s.exit_path == "fail",
        )
        result = await GraphEngine(compiled).run_async(ctx)
    """

    g: Graph[S] = Graph()
    g.add_node(
        "body",
        _RetryBodyWrapper(
            body=body_node,
            max_retries=max_retries,
            is_failure=is_failure,
        ),
    )
    g.add_edge(GraphNode.START, "body")
    g.add_edge("body", "body")
    g.add_edge("body", GraphNode.END)
    return g.compile(max_iterations=max_retries + 5)


__all__ = [
    "RetryNode",
    "build_retry_graph",
]
