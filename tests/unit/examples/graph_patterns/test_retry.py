"""Tests for `examples/graph_patterns/retry.py`.

Verifies:
- `RetryNode` succeeds on first attempt when `is_failure` returns `False`
  (no retry).
- `RetryNode` retries up to `max_retries` times, then succeeds on a later
  attempt.
- `RetryNode` exhausts `max_retries` and returns `transition="failed"`.
- `build_retry_graph` self-loop topology exits correctly on success
  (`transition="success"` -> END).
- `build_retry_graph` self-loop topology exits correctly on failure after
  exhausting retries (`transition="failed"` -> END).
- `build_retry_graph` does not raise `GraphRecursionError` when
  `max_iterations` is set appropriately.

Tests assert observable state (attempt count, exit path label, counter
value) via `GraphEngine.run_async(ctx)` — per the TDD-at-the-execution-seam
guidance in the task spec.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated

# Add `examples/` to sys.path so `graph_patterns` is importable as a
# top-level package. Mirrors the pattern in test_conditional.py.
_EXAMPLES_DIR = Path(__file__).parent.parent.parent.parent.parent / "examples"
if str(_EXAMPLES_DIR) not in sys.path:
    sys.path.insert(0, str(_EXAMPLES_DIR))

from graph_patterns import (  # noqa: E402
    RetryNode,
    build_retry_graph,
)

from modex_graph import (  # noqa: E402
    Graph,
    GraphContext,
    GraphEngine,
    GraphNode,
    GraphRuntime,
    GraphState,
    LastValue,
    Node,
    NodeResult,
)

# ─── Shared state types ────────────────────────────────────────────────


class RetryState(GraphState):
    """State for RetryNode tests: tracks body call count + exit path."""

    attempts: Annotated[int, LastValue] = 0
    exit_path: Annotated[str, LastValue] = ""


class TopologyRetryState(GraphState):
    """State for build_retry_graph tests: counter + body call count + exit."""

    retries: Annotated[int, LastValue] = 0
    attempts: Annotated[int, LastValue] = 0
    exit_path: Annotated[str, LastValue] = ""


# ─── Shared body nodes ────────────────────────────────────────────────


class FlakyBody(Node[RetryState]):
    """Body that fails for the first `fail_count` calls, then succeeds.

    Uses imperative state mutations (no state_update) — intermediate
    mutations persist across retries. Signals failure via
    `transition="fail"`; success via `transition="ok"`.
    """

    def __init__(self, fail_count: int) -> None:
        self.fail_count = fail_count

    def execute(self, ctx: GraphContext[RetryState]) -> NodeResult:
        ctx.state.attempts += 1
        if ctx.state.attempts <= self.fail_count:
            return NodeResult(transition="fail")
        return NodeResult(transition="ok")


class AlwaysFailBody(Node[TopologyRetryState]):
    """Body that always fails — used for topology retry exhaustion tests."""

    def execute(self, ctx: GraphContext[TopologyRetryState]) -> NodeResult:
        ctx.state.attempts += 1
        return NodeResult(transition="fail")


class FailNTimesBody(Node[TopologyRetryState]):
    """Body that fails for the first `fail_count` calls, then succeeds."""

    def __init__(self, fail_count: int) -> None:
        self.fail_count = fail_count

    def execute(self, ctx: GraphContext[TopologyRetryState]) -> NodeResult:
        ctx.state.attempts += 1
        if ctx.state.attempts <= self.fail_count:
            return NodeResult(transition="fail")
        return NodeResult(transition="ok")


class MarkExitNode(Node[RetryState]):
    """Terminal marker node — stamps `exit_path` with its label."""

    def __init__(self, label: str) -> None:
        self.label = label

    def execute(self, ctx: GraphContext[RetryState]) -> NodeResult:
        ctx.state.exit_path = self.label
        return NodeResult()


# ─── Helpers ──────────────────────────────────────────────────────────


def _is_failure(result: NodeResult) -> bool:
    """Failure predicate: body signals failure via transition='fail'."""
    return result.transition == "fail"


def _make_retry_ctx(state: RetryState | None = None) -> GraphContext[RetryState]:
    return GraphContext(
        state=state if state is not None else RetryState(),
        runtime=GraphRuntime(),
    )


def _make_topology_ctx(
    state: TopologyRetryState | None = None,
) -> GraphContext[TopologyRetryState]:
    return GraphContext(
        state=state if state is not None else TopologyRetryState(),
        runtime=GraphRuntime(),
    )


# ─── RetryNode tests ──────────────────────────────────────────────────


class TestRetryNode:
    """RetryNode: synchronous retry within a single execute call.

    Graph topology for all tests::

        START -> retry -> failed_exit (reason="failed") -> END
                     -> default_exit (reason=None)       -> END

    On success, RetryNode returns the body's NodeResult (transition="ok").
    No "ok" edge exists, so the engine falls through to the default edge
    -> default_exit. On exhaustion, RetryNode returns transition="failed"
    -> failed_exit. The `exit_path` state field records which path was
    taken, making the transition observable.
    """

    def _build_graph(
        self,
        body: FlakyBody,
        max_retries: int,
    ) -> Graph[RetryState]:
        g: Graph[RetryState] = Graph()
        g.add_node("retry", RetryNode(body, max_retries, _is_failure))
        g.add_node("failed_exit", MarkExitNode("failed"))
        g.add_node("default_exit", MarkExitNode("success"))
        g.add_edge(GraphNode.START, "retry")
        g.add_edge("retry", "failed_exit", reason="failed")
        g.add_edge("retry", "default_exit", reason=None)
        g.add_edge("failed_exit", GraphNode.END)
        g.add_edge("default_exit", GraphNode.END)
        return g

    async def test_succeeds_on_first_attempt_no_retry(self) -> None:
        """is_failure returns False on the first attempt -> no retry."""
        body = FlakyBody(fail_count=0)
        compiled = self._build_graph(body, max_retries=3).compile()

        ctx = _make_retry_ctx()
        result = await GraphEngine(compiled).run_async(ctx)

        assert result.attempts == 1
        assert result.exit_path == "success"

    async def test_retries_then_succeeds_on_later_attempt(self) -> None:
        """Body fails twice, succeeds on third attempt within max_retries."""
        body = FlakyBody(fail_count=2)
        compiled = self._build_graph(body, max_retries=5).compile()

        ctx = _make_retry_ctx()
        result = await GraphEngine(compiled).run_async(ctx)

        assert result.attempts == 3
        assert result.exit_path == "success"

    async def test_exhausts_max_retries_and_returns_failed_transition(self) -> None:
        """All max_retries+1 attempts fail -> transition='failed' -> failed_exit."""
        body = FlakyBody(fail_count=100)
        compiled = self._build_graph(body, max_retries=2).compile()

        ctx = _make_retry_ctx()
        result = await GraphEngine(compiled).run_async(ctx)

        # max_retries=2 -> body called max_retries+1 = 3 times.
        assert result.attempts == 3
        assert result.exit_path == "failed"


# ─── build_retry_graph tests ──────────────────────────────────────────


class TestBuildRetryGraph:
    """build_retry_graph: topology retry via self-loop.

    Topology (built internally by build_retry_graph)::

        START -> body -> body (self-loop, reason="retry")
                     -> END (reason="success")
                     -> END (reason="failed")

    The wrapper increments `retries` on each execution and decides the
    transition: "success" (not is_failure), "retry" (counter < max_retries),
    or "failed" (counter >= max_retries).
    """

    async def test_exits_correctly_on_success(self) -> None:
        """Body succeeds on first attempt -> transition='success' -> END."""
        body = FailNTimesBody(fail_count=0)
        compiled = build_retry_graph(
            body_node=body,
            max_retries=3,
            is_failure=_is_failure,
            counter_state_field="retries",
        )

        ctx = _make_topology_ctx()
        result = await GraphEngine(compiled).run_async(ctx)

        # Body called once; wrapper increments counter to 1; not a failure
        # -> transition="success" -> END.
        assert result.attempts == 1
        assert result.retries == 1

    async def test_exits_correctly_on_failure_after_exhausting_retries(self) -> None:
        """Body always fails; counter reaches max_retries -> transition='failed'."""
        body = AlwaysFailBody()
        compiled = build_retry_graph(
            body_node=body,
            max_retries=3,
            is_failure=_is_failure,
            counter_state_field="retries",
        )

        ctx = _make_topology_ctx()
        result = await GraphEngine(compiled).run_async(ctx)

        # max_retries=3: body called 3 times (counter 1->2->3). At counter=3,
        # 3 >= 3 -> transition="failed" -> END.
        assert result.attempts == 3
        assert result.retries == 3

    async def test_does_not_raise_recursion_error_with_appropriate_max_iterations(
        self,
    ) -> None:
        """max_iterations=max_retries+5 is sufficient — no GraphRecursionError.

        Uses a larger max_retries to stress the safety net margin. The body
        fails until the last allowed retry, then still fails — exercising
        the maximum loop count (max_retries iterations) well within the
        max_iterations=max_retries+5 safety net.
        """
        body = AlwaysFailBody()
        compiled = build_retry_graph(
            body_node=body,
            max_retries=10,
            is_failure=_is_failure,
            counter_state_field="retries",
        )

        ctx = _make_topology_ctx()
        # If max_iterations were too low, GraphRecursionError would be
        # raised here. Reaching the assertion proves the safety net margin
        # is sufficient.
        result = await GraphEngine(compiled).run_async(ctx)

        assert result.attempts == 10
        assert result.retries == 10
