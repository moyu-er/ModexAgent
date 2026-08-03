"""Tests for ``examples/graph_patterns/retry.py``.

Verifies:
- ``RetryNode`` succeeds on first attempt when ``is_failure`` returns ``False``.
- ``RetryNode`` retries up to ``max_retries`` times, then succeeds.
- ``RetryNode`` exhausts ``max_retries`` and delivers to ``failure_target``.
- ``build_retry_graph`` self-loop topology exits correctly on success.
- ``build_retry_graph`` self-loop topology exits correctly on failure after
  exhausting retries.
- ``build_retry_graph`` does not raise ``GraphRecursionError`` when
  ``max_iterations`` is set appropriately.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated

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
    IntegratedInput,
    LastValue,
    Node,
    NodeResult,
)


class RetryState(GraphState):
    """State for RetryNode tests: tracks body call count + exit path."""

    attempts: Annotated[int, LastValue] = 0
    exit_path: Annotated[str, LastValue] = ""


class TopologyRetryState(GraphState):
    """State for build_retry_graph tests: counter + body call count."""

    retries: Annotated[int, LastValue] = 0
    attempts: Annotated[int, LastValue] = 0
    exit_path: Annotated[str, LastValue] = ""


class FlakyBody(Node[RetryState]):
    """Body that fails for the first ``fail_count`` calls, then succeeds.

    Signals failure via imperative ``ctx.state.exit_path = "fail"``.
    """

    def __init__(self, fail_count: int) -> None:
        self.fail_count = fail_count

    def execute(self, ctx: GraphContext[RetryState], integrated_input: IntegratedInput) -> NodeResult:
        ctx.state.attempts += 1
        if ctx.state.attempts <= self.fail_count:
            ctx.state.exit_path = "fail"
        else:
            ctx.state.exit_path = "ok"
        return NodeResult()


class AlwaysFailBody(Node[TopologyRetryState]):
    """Body that always fails."""

    def execute(self, ctx: GraphContext[TopologyRetryState], integrated_input: IntegratedInput) -> NodeResult:
        ctx.state.attempts += 1
        ctx.state.exit_path = "fail"
        return NodeResult()


class FailNTimesBody(Node[TopologyRetryState]):
    """Body that fails for the first ``fail_count`` calls, then succeeds."""

    def __init__(self, fail_count: int) -> None:
        self.fail_count = fail_count

    def execute(self, ctx: GraphContext[TopologyRetryState], integrated_input: IntegratedInput) -> NodeResult:
        ctx.state.attempts += 1
        if ctx.state.attempts <= self.fail_count:
            ctx.state.exit_path = "fail"
        else:
            ctx.state.exit_path = "ok"
        return NodeResult()


class MarkExitNode(Node[RetryState]):
    """Terminal marker node -- stamps ``exit_path`` with its label."""

    def __init__(self, label: str) -> None:
        self.label = label

    def execute(self, ctx: GraphContext[RetryState], integrated_input: IntegratedInput) -> NodeResult:
        ctx.state.exit_path = self.label
        self.deliver(None, None, ctx)
        return NodeResult()


def _is_retry_failure(state: RetryState) -> bool:
    return state.exit_path == "fail"


def _is_topology_failure(state: TopologyRetryState) -> bool:
    return state.exit_path == "fail"


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


class TestRetryNode:
    """RetryNode: synchronous retry within a single execute call."""

    def _build_graph(
        self,
        body: FlakyBody,
        max_retries: int,
    ) -> Graph[RetryState]:
        g: Graph[RetryState] = Graph()
        g.add_node("retry", RetryNode(body, max_retries, _is_retry_failure, success_target="default_exit", failure_target="failed_exit"))
        g.add_node("failed_exit", MarkExitNode("failed"))
        g.add_node("default_exit", MarkExitNode("success"))
        g.add_edge(GraphNode.START, "retry")
        g.add_edge("retry", "failed_exit")
        g.add_edge("retry", "default_exit")
        g.add_edge("failed_exit", GraphNode.END)
        g.add_edge("default_exit", GraphNode.END)
        return g

    async def test_succeeds_on_first_attempt_no_retry(self) -> None:
        body = FlakyBody(fail_count=0)
        compiled = self._build_graph(body, max_retries=3).compile()
        ctx = _make_retry_ctx()
        result = await GraphEngine(compiled).run_async(ctx)
        assert result.attempts == 1
        assert result.exit_path == "success"

    async def test_retries_then_succeeds_on_later_attempt(self) -> None:
        body = FlakyBody(fail_count=2)
        compiled = self._build_graph(body, max_retries=5).compile()
        ctx = _make_retry_ctx()
        result = await GraphEngine(compiled).run_async(ctx)
        assert result.attempts == 3
        assert result.exit_path == "success"

    async def test_exhausts_max_retries_and_delivers_to_failure_target(self) -> None:
        body = FlakyBody(fail_count=100)
        compiled = self._build_graph(body, max_retries=2).compile()
        ctx = _make_retry_ctx()
        result = await GraphEngine(compiled).run_async(ctx)
        assert result.attempts == 3
        assert result.exit_path == "failed"


class TestBuildRetryGraph:
    """build_retry_graph: topology retry via self-loop."""

    async def test_exits_correctly_on_success(self) -> None:
        body = FailNTimesBody(fail_count=0)
        compiled = build_retry_graph(
            body_node=body,
            max_retries=3,
            is_failure=_is_topology_failure,
            counter_state_field="retries",
        )
        ctx = _make_topology_ctx()
        result = await GraphEngine(compiled).run_async(ctx)
        assert result.attempts == 1
        assert result.retries == 1

    async def test_exits_correctly_on_failure_after_exhausting_retries(self) -> None:
        body = AlwaysFailBody()
        compiled = build_retry_graph(
            body_node=body,
            max_retries=3,
            is_failure=_is_topology_failure,
            counter_state_field="retries",
        )
        ctx = _make_topology_ctx()
        result = await GraphEngine(compiled).run_async(ctx)
        assert result.attempts == 3
        assert result.retries == 3

    async def test_does_not_raise_recursion_error_with_appropriate_max_iterations(
        self,
    ) -> None:
        body = AlwaysFailBody()
        compiled = build_retry_graph(
            body_node=body,
            max_retries=10,
            is_failure=_is_topology_failure,
            counter_state_field="retries",
        )
        ctx = _make_topology_ctx()
        result = await GraphEngine(compiled).run_async(ctx)
        assert result.attempts == 10
        assert result.retries == 10