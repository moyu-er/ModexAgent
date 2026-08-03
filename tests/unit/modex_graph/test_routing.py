"""Routing tests: default edge resolution + deliver-based routing."""
from __future__ import annotations

from helpers import CounterState, make_ctx

from modex_graph import (
    Graph,
    GraphContext,
    GraphEngine,
    GraphNode,
    IntegratedInput,
    Node,
    NodeResult,
)


class _RecordNameNode(Node[CounterState]):
    """Records its name into state.messages, delivers to default target."""

    def execute(self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput) -> NodeResult:
        ctx.state.messages = ctx.state.messages + [self.name]
        self.deliver(None, None, ctx)
        return NodeResult()


class _StateUpdateNode(Node[CounterState]):
    """Returns NodeResult(state_update={"messages": [label]})."""

    def __init__(self, label: str) -> None:
        self.label = label

    def execute(self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput) -> NodeResult:
        return NodeResult(state_update={"messages": [self.label]})


class TestDefaultEdgeFallback:
    """Default edge is the fallback when next_node=None."""

    async def test_default_edge_used_when_no_explicit_target(self) -> None:
        g: Graph[CounterState] = Graph()
        g.add_node("start", _RecordNameNode())
        g.add_node("next", _RecordNameNode())
        g.add_edge(GraphNode.START, "start")
        g.add_edge("start", "next")
        g.add_edge("next", GraphNode.END)
        compiled = g.compile()
        ctx = make_ctx(CounterState())
        result = await GraphEngine(compiled).run_async(ctx)
        assert result.messages == ["start", "next"]
