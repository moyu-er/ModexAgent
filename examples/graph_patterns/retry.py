"""Retry graph patterns over `modex_graph`.

Two forms of retry, exercising different parts of the retained API:

1. ``RetryNode[S]`` — synchronous retry within a single ``execute`` call.
   The node wraps a body ``Node`` and calls ``body.execute(ctx)`` up to
   ``max_retries + 1`` times. If ``is_failure(result)`` returns ``False``,
   the successful ``NodeResult`` is returned immediately; if all attempts
   fail, ``NodeResult(transition="failed")`` is returned. This form
   exercises ``Node`` composition (a ``Node`` wrapping another ``Node``)
   without involving graph topology.

2. ``build_retry_graph(...)`` — topology retry via self-loop. The body node
   is wired to itself with ``transition="retry"`` (self-loop edge), and to
   ``GraphNode.END`` with ``transition="success"`` and ``transition="failed"``.
   A counter field in state tracks attempt count; when the counter reaches
   ``max_retries``, the wrapper returns ``transition="failed"`` instead of
   ``transition="retry"``. This form exercises self-loop edges,
   ``transition``-based routing, and ``max_iterations`` as a panic safety
   net (set to ``max_retries + 5``).

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
    Node,
    NodeResult,
)


class RetryNode[S: GraphState](Node[S]):
    """Synchronous retry within a single ``execute`` call.

    Calls ``body.execute(ctx)`` up to ``max_retries + 1`` times. On each
    attempt, the body's ``state_update`` (if any) is applied to ``ctx.state``,
    matching engine behavior so intermediate mutations persist across
    retries. If ``is_failure(result)`` returns ``False``, the body's
    ``NodeResult`` is returned immediately. If all attempts fail,
    ``NodeResult(transition="failed")`` is returned.

    The body node is NOT registered with a graph — it is called directly in
    ``execute``. Its ``name`` attribute may be unset; that is fine because
    ``RetryNode`` uses it as a collaborator, not a graph node.

    The body's ``execute`` must be synchronous (``def``, not ``async def``).
    For async bodies, use ``build_retry_graph`` instead — the engine's
    ``inspect.isawaitable`` unification handles async nodes in graph
    topology.

    Example::

        g.add_node("retry", RetryNode(body, max_retries=3, is_failure=is_fail))
        g.add_edge(GraphNode.START, "retry")
        g.add_edge("retry", GraphNode.END, reason="failed")
        g.add_edge("retry", GraphNode.END, reason=None)  # default for success
    """

    def __init__(
        self,
        body: Node[S],
        max_retries: int,
        is_failure: Callable[[NodeResult], bool],
    ) -> None:
        self.body = body
        self.max_retries = max_retries
        self.is_failure = is_failure

    def execute(self, ctx: GraphContext[S]) -> NodeResult:
        result: NodeResult
        for _ in range(self.max_retries + 1):
            raw = self.body.execute(ctx)
            if isinstance(raw, NodeResult):
                result = raw
            else:
                raise TypeError(
                    "RetryNode requires a synchronous body node "
                    "(body.execute must return NodeResult directly, not a "
                    "coroutine). Use build_retry_graph for async bodies."
                )
            if result.state_update is not None:
                ctx.state.apply_state_update(result.state_update)
            if not self.is_failure(result):
                return result
        return NodeResult(transition="failed")


class _RetryBodyWrapper[S: GraphState](Node[S]):
    """Internal retry-aware wrapper around a user-provided body node.

    Used by ``build_retry_graph``. Not part of the public API.

    On each execution:
    1. Calls ``body.execute(ctx)`` to get the ``NodeResult``.
    2. Applies the body's ``state_update`` (if any) to ``ctx.state``.
    3. Increments ``ctx.state.<counter_state_field>`` by 1.
    4. If ``not is_failure(result)``: returns ``transition="success"``.
    5. If ``is_failure(result)`` and counter < ``max_retries``: returns
       ``transition="retry"`` (self-loop).
    6. If ``is_failure(result)`` and counter >= ``max_retries``: returns
       ``transition="failed"``.

    The user's body node does not need to know about the counter — the
    wrapper manages it. ``getattr``/``setattr`` are used for the dynamic
    counter field access (legitimate extension boundary per rule 6).
    """

    def __init__(
        self,
        body: Node[S],
        max_retries: int,
        is_failure: Callable[[NodeResult], bool],
        counter_state_field: str,
    ) -> None:
        self.body = body
        self.max_retries = max_retries
        self.is_failure = is_failure
        self.counter_state_field = counter_state_field

    def execute(self, ctx: GraphContext[S]) -> NodeResult:
        raw = self.body.execute(ctx)
        if isinstance(raw, NodeResult):
            result = raw
        else:
            raise TypeError(
                "_RetryBodyWrapper requires a synchronous body node "
                "(body.execute must return NodeResult directly, not a "
                "coroutine)."
            )
        if result.state_update is not None:
            ctx.state.apply_state_update(result.state_update)
        current = getattr(ctx.state, self.counter_state_field)
        next_count = current + 1
        setattr(ctx.state, self.counter_state_field, next_count)
        if not self.is_failure(result):
            return NodeResult(transition="success")
        if next_count < self.max_retries:
            return NodeResult(transition="retry")
        return NodeResult(transition="failed")


def build_retry_graph[S: GraphState](
    body_node: Node[S],
    max_retries: int,
    is_failure: Callable[[NodeResult], bool],
    counter_state_field: str,
) -> CompiledGraph[S]:
    """Build a retry-via-self-loop topology and compile it.

    Topology::

        START -> body -> body (self-loop, reason="retry")
                     -> END (reason="success")
                     -> END (reason="failed")

    The user-provided ``body_node`` is wrapped in a ``_RetryBodyWrapper``
    that manages the counter and decides the transition. The body node does
    NOT need to know about the counter — the wrapper handles it.

    The graph is compiled with ``max_iterations=max_retries + 5`` as a panic
    safety net (allows the retry loop plus margin). If the business-level
    retry logic is correct, the loop exits via ``transition="success"`` or
    ``transition="failed"`` before the safety net fires.

    Returns the compiled graph — pass to ``GraphEngine`` to execute.

    Example::

        compiled = build_retry_graph(
            body_node=MyBodyNode(),
            max_retries=3,
            is_failure=lambda r: r.transition == "fail",
            counter_state_field="retries",
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
            counter_state_field=counter_state_field,
        ),
    )
    g.add_edge(GraphNode.START, "body")
    g.add_edge("body", "body", reason="retry")
    g.add_edge("body", GraphNode.END, reason="success")
    g.add_edge("body", GraphNode.END, reason="failed")
    return g.compile(max_iterations=max_retries + 5)


__all__ = [
    "RetryNode",
    "build_retry_graph",
]
