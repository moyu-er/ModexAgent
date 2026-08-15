# ruff: noqa: ANN401
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

The retry counter lives in ``ctx.scratch`` (the current node's scoped
region of ``ctx.state.node_scratch[self.node_id]``, graph-run-scoped,
resets per run). Tests assert on ``result.attempts`` (body call count
written by the body node itself).
"""

from __future__ import annotations

import sys
from importlib import import_module
from pathlib import Path
from typing import Any

from modex_graph import (
    Graph,
    GraphContext,
    GraphEngine,
    GraphNode,
    GraphPersistenceCoordinator,
    GraphRuntime,
    GraphState,
    IntegratedInput,
    Node,
    NullDeliverStoreFactory,
    NullGraphInstanceStore,
    NullNodeStateStore,
)
from modex_graph.scheduler.bootstrap import BootstrapMode

_EXAMPLES_DIR = Path(__file__).parent.parent.parent.parent.parent / "examples"
if str(_EXAMPLES_DIR) not in sys.path:
    sys.path.insert(0, str(_EXAMPLES_DIR))

_retry = import_module("graph_patterns.retry")
RetryNode = _retry.RetryNode
build_retry_graph = _retry.build_retry_graph


class _AutoRegCoord(GraphPersistenceCoordinator):
    def collect_consumable_delivers(
        self, node_id: str, invocation_id: int
    ) -> list[Any]:
        if self.get_deliver_store(node_id) is None:
            self.register_node(node_id)
        return super().collect_consumable_delivers(node_id, invocation_id)

    def route_deliver(
        self,
        target_node_id: str,
        content: Any,
        source_node_id: str,
        source_invocation_id: int,
        source_node_name: str | None = None,
        stage: bool = False,
    ) -> int | None:
        if self.get_deliver_store(target_node_id) is None:
            self.register_node(target_node_id)
        return super().route_deliver(
            target_node_id,
            content,
            source_node_id,
            source_invocation_id,
            source_node_name,
            stage,
        )


def _make_coordinator() -> _AutoRegCoord:
    return _AutoRegCoord(
        graph_instance_id=0,
        instance_store=NullGraphInstanceStore(),
        node_state_store=NullNodeStateStore(0),
        default_deliver_store_factory=NullDeliverStoreFactory(),
    )


class RetryState(GraphState):
    """State for RetryNode tests: tracks body call count + exit path."""

    attempts: int = 0
    exit_path: str = ""


class TopologyRetryState(GraphState):
    """State for build_retry_graph tests: body call count + exit path.

    The retry counter is now an instance variable on ``_RetryBodyWrapper``
    (``self._attempt_count``), not a state field. ``attempts`` tracks how
    many times the body was called (written by the body node itself).
    """

    attempts: int = 0
    exit_path: str = ""


class FlakyBody(Node[RetryState]):
    """Body that fails for the first ``fail_count`` calls, then succeeds.

    Signals failure via imperative ``ctx.state.exit_path = "fail"``.
    """

    def __init__(self, fail_count: int) -> None:
        self.fail_count = fail_count

    async def execute(
        self, ctx: GraphContext[RetryState], integrated_input: IntegratedInput
    ) -> None:
        ctx.state.attempts += 1
        if ctx.state.attempts <= self.fail_count:
            ctx.state.exit_path = "fail"
        else:
            ctx.state.exit_path = "ok"
        return None


class AlwaysFailBody(Node[TopologyRetryState]):
    """Body that always fails."""

    async def execute(
        self, ctx: GraphContext[TopologyRetryState], integrated_input: IntegratedInput
    ) -> None:
        ctx.state.attempts += 1
        ctx.state.exit_path = "fail"
        return None


class FailNTimesBody(Node[TopologyRetryState]):
    """Body that fails for the first ``fail_count`` calls, then succeeds."""

    def __init__(self, fail_count: int) -> None:
        self.fail_count = fail_count

    async def execute(
        self, ctx: GraphContext[TopologyRetryState], integrated_input: IntegratedInput
    ) -> None:
        ctx.state.attempts += 1
        if ctx.state.attempts <= self.fail_count:
            ctx.state.exit_path = "fail"
        else:
            ctx.state.exit_path = "ok"
        return None


class MarkExitNode(Node[RetryState]):
    """Terminal marker node -- stamps ``exit_path`` with its label."""

    def __init__(self, label: str) -> None:
        self.label = label

    async def execute(
        self, ctx: GraphContext[RetryState], integrated_input: IntegratedInput
    ) -> None:
        ctx.state.exit_path = self.label
        self.deliver(None, None, ctx)
        return None


def _is_retry_failure(state: RetryState) -> bool:
    return state.exit_path == "fail"


def _is_topology_failure(state: TopologyRetryState) -> bool:
    return state.exit_path == "fail"


def _make_retry_ctx(state: RetryState | None = None) -> GraphContext[RetryState]:
    return GraphContext(
        state=state if state is not None else RetryState(),
        runtime=GraphRuntime(),
        coordinator=_make_coordinator(),
    )


def _make_topology_ctx(
    state: TopologyRetryState | None = None,
) -> GraphContext[TopologyRetryState]:
    return GraphContext(
        state=state if state is not None else TopologyRetryState(),
        runtime=GraphRuntime(),
        coordinator=_make_coordinator(),
    )


class TestRetryNode:
    """RetryNode: synchronous retry within a single execute call."""

    def _build_graph(
        self,
        body: FlakyBody,
        max_retries: int,
    ) -> Graph[RetryState]:
        g: Graph[RetryState] = Graph()
        g.add_node(
            "retry",
            RetryNode(
                body,
                max_retries,
                _is_retry_failure,
                success_target="default_exit",
                failure_target="failed_exit",
            ),
        )
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
        result = await GraphEngine(compiled).run_async(ctx, mode=BootstrapMode.FRESH)
        assert result.attempts == 1
        assert result.exit_path == "success"

    async def test_retries_then_succeeds_on_later_attempt(self) -> None:
        body = FlakyBody(fail_count=2)
        compiled = self._build_graph(body, max_retries=5).compile()
        ctx = _make_retry_ctx()
        result = await GraphEngine(compiled).run_async(ctx, mode=BootstrapMode.FRESH)
        assert result.attempts == 3
        assert result.exit_path == "success"

    async def test_exhausts_max_retries_and_delivers_to_failure_target(self) -> None:
        body = FlakyBody(fail_count=100)
        compiled = self._build_graph(body, max_retries=2).compile()
        ctx = _make_retry_ctx()
        result = await GraphEngine(compiled).run_async(ctx, mode=BootstrapMode.FRESH)
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
        )
        ctx = _make_topology_ctx()
        result = await GraphEngine(compiled).run_async(ctx, mode=BootstrapMode.FRESH)
        assert result.attempts == 1

    async def test_exits_correctly_on_failure_after_exhausting_retries(self) -> None:
        body = AlwaysFailBody()
        compiled = build_retry_graph(
            body_node=body,
            max_retries=3,
            is_failure=_is_topology_failure,
        )
        ctx = _make_topology_ctx()
        result = await GraphEngine(compiled).run_async(ctx, mode=BootstrapMode.FRESH)
        assert result.attempts == 3

    async def test_does_not_raise_recursion_error_with_appropriate_max_iterations(
        self,
    ) -> None:
        body = AlwaysFailBody()
        compiled = build_retry_graph(
            body_node=body,
            max_retries=10,
            is_failure=_is_topology_failure,
        )
        ctx = _make_topology_ctx()
        result = await GraphEngine(compiled).run_async(ctx, mode=BootstrapMode.FRESH)
        assert result.attempts == 10
