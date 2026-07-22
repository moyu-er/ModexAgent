"""Subgraph-as-node execution test (Graph-is-a-Node, ADR-0033 D8)."""
from __future__ import annotations

from helpers import CounterState, make_ctx

from modex_graph import (
    Graph,
    GraphContext,
    GraphEngine,
    GraphNode,
    Node,
    NodeResult,
)


class TestSubgraphAsNode:
    """CompiledGraph is a Node — a graph can be embedded as a node in another graph."""

    async def test_subgraph_executes_within_parent_graph(self) -> None:
        """An inner graph (compiled) is used as a node in the outer graph."""

        class IncrementNode(Node[CounterState]):
            def execute(self, ctx: GraphContext[CounterState]) -> NodeResult:
                ctx.state.count += 1
                return NodeResult(transition="done")

        # Inner graph: start → increment → END (adds 1 to count)
        inner: Graph[CounterState] = Graph(name="inner")
        inner.add_node("inc", IncrementNode())
        inner.add_edge(GraphNode.START, "inc")
        inner.add_edge("inc", GraphNode.END, reason="done")
        inner_compiled = inner.compile()

        # Outer graph: start → [inner graph as node] → verify → END
        class VerifyNode(Node[CounterState]):
            def execute(self, ctx: GraphContext[CounterState]) -> NodeResult:
                ctx.state.name = f"count_is_{ctx.state.count}"
                return NodeResult()

        outer: Graph[CounterState] = Graph(name="outer")
        # CompiledGraph is a Node — register it directly.
        outer.add_node("inner", inner_compiled)
        outer.add_node("verify", VerifyNode())
        outer.add_edge(GraphNode.START, "inner")
        outer.add_edge("inner", "verify", reason=None)
        outer.add_edge("verify", GraphNode.END)
        outer_compiled = outer.compile()

        ctx = make_ctx(CounterState(count=0, name=""))
        result = await GraphEngine(outer_compiled).run_async(ctx)
        # Inner graph ran IncrementNode → count = 1
        # Verify node set name → "count_is_1"
        assert result.count == 1
        assert result.name == "count_is_1"

    async def test_subgraph_shares_parent_context(self) -> None:
        """The subgraph shares ctx.state / ctx.runtime / ctx.user_data with parent."""

        class TagNode(Node[CounterState]):
            def execute(self, ctx: GraphContext[CounterState]) -> NodeResult:
                ctx.state.name = ctx.state.name + "tagged"
                return NodeResult()

        inner: Graph[CounterState] = Graph(name="inner")
        inner.add_node("tag", TagNode())
        inner.add_edge(GraphNode.START, "tag")
        inner.add_edge("tag", GraphNode.END)
        inner_compiled = inner.compile()

        outer: Graph[CounterState] = Graph(name="outer")
        outer.add_node("inner", inner_compiled)
        outer.add_edge(GraphNode.START, "inner")
        outer.add_edge("inner", GraphNode.END)
        outer_compiled = outer.compile()

        ctx = make_ctx(CounterState(name="initial_"))
        result = await GraphEngine(outer_compiled).run_async(ctx)
        # Inner graph's TagNode mutated the shared state.
        assert result.name == "initial_tagged"
