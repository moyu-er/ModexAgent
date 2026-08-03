"""Retry graph patterns over `modex_graph`.

Two forms of retry, exercising different parts of the deliver/submit API:

1. ``RetryNode[S]`` — synchronous retry within a single ``execute`` call.
   The node wraps a body ``Node`` and calls ``body.execute(ctx)`` up to
   ``max_retries + 1`` times. If ``is_failure(ctx.state)`` returns ``False``,
   the node delivers to its default target (success). If all attempts fail,
   the node delivers to ``failure_target`` (exhaustion). This form exercises
   ``Node`` composition without involving graph topology for the retry loop.

2. ``build_retry_graph(...)`` — topology retry via self-loop. The body node
   is wired to itself (self-loop edge) and to ``GraphNode.END``. A counter
   field in state tracks attempt count; when the counter reaches
   ``max_retries``, the wrapper delivers to END instead of back to itself.
   This form exercises self-loop edges and ``max_iterations`` as a panic
   safety net (set to ``max_retries + 5``).

The ``is_failure`` callback takes the state ``S`` and returns ``True`` if
the body failed. The body signals failure via imperative state mutation
(e.g. ``ctx.state.exit_path = "fail"``). The former ``transition``-based
signaling was removed (P3.4b convergence).

IMPORTANT: the body must NOT return ``NodeResult(state_update=...)`` for the
failure signal — ``apply_state_update`` syncs ALL channels back to fields,
which would reset imperative counter mutations. Use imperative state
mutation only.

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
    NodeResult,
)


class RetryNode[S: GraphState](Node[S]):
    """Synchronous retry within a single ``execute`` call.

    Calls ``body.execute(ctx)`` up to ``max_retries + 1`` times. On each
    attempt, the body's ``state_update`` (if any) is applied to ``ctx.state``.
    If ``is_failure(ctx.state)`` returns ``False``, the node delivers to
    ``success_target``. If all attempts fail, the node delivers to
    ``failure_target``.

    The body signals failure via imperative state mutation (e.g.
    ``ctx.state.exit_path = "fail"``). ``is_failure`` reads the state to
    determine if the body failed.

    The body's ``execute`` must be synchronous (``def``, not ``async def``).

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

    def execute(self, ctx: GraphContext[S], integrated_input: IntegratedInput) -> NodeResult:
        for _ in range(self.max_retries + 1):
            raw = self.body.execute(ctx, integrated_input)
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
            if not self.is_failure(ctx.state):
                self.deliver(None, self.success_target, ctx)
                return NodeResult()
        self.deliver(None, self.failure_target, ctx)
        return NodeResult()


class _RetryBodyWrapper[S: GraphState](Node[S]):
    """Internal retry-aware wrapper around a user-provided body node.

    On each execution:
    1. Calls ``body.execute(ctx)`` to get the ``NodeResult``.
    2. Applies the body's ``state_update`` (if any) to ``ctx.state``.
    3. Increments ``ctx.state.<counter_state_field>`` by 1 (imperative).
    4. If ``not is_failure(ctx.state)``: delivers to ``GraphNode.END``.
    5. If ``is_failure(ctx.state)`` and counter < ``max_retries``: delivers
       to ``"body"`` (self-loop).
    6. If ``is_failure(ctx.state)`` and counter >= ``max_retries``: delivers
       to ``GraphNode.END`` (exhaustion).

    The counter is managed via imperative mutation (``setattr``), NOT
    ``state_update`` — ``apply_state_update`` syncs ALL channels, which
    would reset the counter.
    """

    def __init__(
        self,
        body: Node[S],
        max_retries: int,
        is_failure: Callable[[S], bool],
        counter_state_field: str,
    ) -> None:
        self.body = body
        self.max_retries = max_retries
        self.is_failure = is_failure
        self.counter_state_field = counter_state_field

    def execute(self, ctx: GraphContext[S], integrated_input: IntegratedInput) -> NodeResult:
        raw = self.body.execute(ctx, integrated_input)
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
        if not self.is_failure(ctx.state):
            self.deliver(None, GraphNode.END, ctx)
            return NodeResult()
        if next_count < self.max_retries:
            self.deliver(None, "body", ctx)
            return NodeResult()
        self.deliver(None, GraphNode.END, ctx)
        return NodeResult()


def build_retry_graph[S: GraphState](
    body_node: Node[S],
    max_retries: int,
    is_failure: Callable[[S], bool],
    counter_state_field: str,
) -> CompiledGraph[S]:
    """Build a retry-via-self-loop topology and compile it.

    Topology::

        START -> body -> body (self-loop)
                   -> END

    The user-provided ``body_node`` is wrapped in a ``_RetryBodyWrapper``
    that manages the counter and decides whether to self-loop or exit.

    Returns the compiled graph — pass to ``GraphEngine`` to execute.

    Example::

        compiled = build_retry_graph(
            body_node=MyBodyNode(),
            max_retries=3,
            is_failure=lambda s: s.exit_path == "fail",
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
    g.add_edge("body", "body")
    g.add_edge("body", GraphNode.END)
    return g.compile(max_iterations=max_retries + 5)


__all__ = [
    "RetryNode",
    "build_retry_graph",
]

