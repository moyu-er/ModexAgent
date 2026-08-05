# ruff: noqa: ANN401
"""Tests for `examples/graph_patterns/conditional.py`.

Verifies:
- `ConditionalNode` routes to the correct branch based on predicate output
  (predicate returns ``"high"`` -> high branch executes; ``"low"`` -> low).
- `SwitchNode` routes to the first matching case; falls through to `default`
  when no case matches.
- A complete if/else + merge graph (`build_conditional_graph`) produces the
  expected final state — both branches merge into a common downstream node
  via default edges.

Tests assert observable state changes (counter increments + branch labels),
not internal routing fields — per the TDD-at-the-execution-seam
guidance in the task spec.
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

_EXAMPLES_DIR = Path(__file__).parent.parent.parent.parent.parent / "examples"
if str(_EXAMPLES_DIR) not in sys.path:
    sys.path.insert(0, str(_EXAMPLES_DIR))

_conditional = import_module("graph_patterns.conditional")
ConditionalNode = _conditional.ConditionalNode
SwitchNode = _conditional.SwitchNode
build_conditional_graph = _conditional.build_conditional_graph


class _AutoRegCoord(GraphPersistenceCoordinator):
    def collect_consumable_delivers(
        self, node_name: str, invocation_id: int
    ) -> list[Any]:
        if self.get_deliver_store(node_name) is None:
            self.register_node(node_name)
        return super().collect_consumable_delivers(node_name, invocation_id)

    def route_deliver(
        self,
        target_node: str,
        content: Any,
        source_node: str,
        source_invocation_id: int,
    ) -> int | None:
        if target_node != GraphNode.END and self.get_deliver_store(target_node) is None:
            self.register_node(target_node)
        return super().route_deliver(target_node, content, source_node, source_invocation_id)


def _make_coordinator() -> _AutoRegCoord:
    return _AutoRegCoord(
        graph_instance_id=0,
        instance_store=NullGraphInstanceStore(),
        node_state_store=NullNodeStateStore(0),
        default_deliver_store_factory=NullDeliverStoreFactory(),
    )


class BranchState(GraphState):
    """Test state: a counter and the label of the last-executed branch."""

    count: int = 0
    last_branch: str = ""


class AddNode(Node[BranchState]):
    """Sync node that increments `count` by `amount` and stamps `last_branch`."""

    def __init__(self, amount: int, label: str) -> None:
        self.amount = amount
        self.label = label

    async def execute(
        self, ctx: GraphContext[BranchState], integrated_input: IntegratedInput
    ) -> None:
        ctx.state.count += self.amount
        ctx.state.last_branch = self.label
        self.deliver(None, None, ctx)
        return None


def make_ctx(state: BranchState | None = None) -> GraphContext[BranchState]:
    """Build a GraphContext with BranchState + no-op runtime."""
    return GraphContext(
        state=state if state is not None else BranchState(),
        runtime=GraphRuntime(),
        coordinator=_make_coordinator(),
    )


class TestConditionalNode:
    """ConditionalNode routes via predicate output -> static edge."""

    def _build_graph(self) -> Graph[BranchState]:
        """START -> decide -> (high|low) -> END."""
        g: Graph[BranchState] = Graph()
        g.add_node(
            "decide",
            ConditionalNode(lambda s: "high" if s.count > 5 else "low"),
        )
        g.add_node("high", AddNode(amount=10, label="high"))
        g.add_node("low", AddNode(amount=1, label="low"))
        g.add_edge(GraphNode.START, "decide")
        g.add_edge("decide", "high")
        g.add_edge("decide", "low")
        g.add_edge("high", GraphNode.END)
        g.add_edge("low", GraphNode.END)
        return g

    async def test_routes_to_high_branch_when_predicate_returns_high(self) -> None:
        compiled = self._build_graph().compile()
        ctx = make_ctx(BranchState(count=10))
        result = await GraphEngine(compiled).run_async(ctx)

        assert result.count == 20  # 10 (start) + 10 (high branch)
        assert result.last_branch == "high"

    async def test_routes_to_low_branch_when_predicate_returns_low(self) -> None:
        compiled = self._build_graph().compile()
        ctx = make_ctx(BranchState(count=0))
        result = await GraphEngine(compiled).run_async(ctx)

        assert result.count == 1  # 0 (start) + 1 (low branch)
        assert result.last_branch == "low"


class TestSwitchNode:
    """SwitchNode routes to the first matching case; default otherwise."""

    def _build_graph(self) -> Graph[BranchState]:
        """START -> switch -> (a|b|c|d) -> END."""
        g: Graph[BranchState] = Graph()
        g.add_node(
            "switch",
            SwitchNode(
                cases={
                    "a": lambda s: s.count < 5,
                    "b": lambda s: s.count < 10,
                    "c": lambda s: s.count < 20,
                },
                default="d",
            ),
        )
        g.add_node("a", AddNode(amount=1, label="a"))
        g.add_node("b", AddNode(amount=2, label="b"))
        g.add_node("c", AddNode(amount=3, label="c"))
        g.add_node("d", AddNode(amount=4, label="d"))
        g.add_edge(GraphNode.START, "switch")
        g.add_edge("switch", "a")
        g.add_edge("switch", "b")
        g.add_edge("switch", "c")
        g.add_edge("switch", "d")
        g.add_edge("a", GraphNode.END)
        g.add_edge("b", GraphNode.END)
        g.add_edge("c", GraphNode.END)
        g.add_edge("d", GraphNode.END)
        return g

    async def test_routes_to_first_matching_case(self) -> None:
        compiled = self._build_graph().compile()

        # count=0: "a" (< 5) matches first.
        ctx_a = make_ctx(BranchState(count=0))
        result_a = await GraphEngine(compiled).run_async(ctx_a)
        assert result_a.last_branch == "a"
        assert result_a.count == 1  # 0 + 1

        # count=7: "a" (< 5) fails, "b" (< 10) matches.
        ctx_b = make_ctx(BranchState(count=7))
        result_b = await GraphEngine(compiled).run_async(ctx_b)
        assert result_b.last_branch == "b"
        assert result_b.count == 9  # 7 + 2

        # count=15: "a" and "b" fail, "c" (< 20) matches.
        ctx_c = make_ctx(BranchState(count=15))
        result_c = await GraphEngine(compiled).run_async(ctx_c)
        assert result_c.last_branch == "c"
        assert result_c.count == 18  # 15 + 3

    async def test_falls_through_to_default_when_no_case_matches(self) -> None:
        compiled = self._build_graph().compile()

        # count=100: no case matches -> default "d".
        ctx = make_ctx(BranchState(count=100))
        result = await GraphEngine(compiled).run_async(ctx)

        assert result.last_branch == "d"
        assert result.count == 104  # 100 + 4


class TestConditionalGraphBuilder:
    """build_conditional_graph wires a complete if/else + merge topology."""

    def _build_graph(self) -> Graph[BranchState]:
        return build_conditional_graph(
            predicate=lambda s: "high" if s.count > 5 else "low",
            high_branch=AddNode(amount=100, label="high"),
            low_branch=AddNode(amount=1, label="low"),
            merge=AddNode(amount=1000, label="merge"),
        )

    async def test_high_branch_merges_into_downstream(self) -> None:
        compiled = self._build_graph().compile()
        ctx = make_ctx(BranchState(count=10))
        result = await GraphEngine(compiled).run_async(ctx)

        # 10 (start) -> high adds 100 -> merge adds 1000 = 1110
        assert result.count == 1110
        assert result.last_branch == "merge"

    async def test_low_branch_merges_into_downstream(self) -> None:
        compiled = self._build_graph().compile()
        ctx = make_ctx(BranchState(count=0))
        result = await GraphEngine(compiled).run_async(ctx)

        # 0 (start) -> low adds 1 -> merge adds 1000 = 1001
        assert result.count == 1001
        assert result.last_branch == "merge"
