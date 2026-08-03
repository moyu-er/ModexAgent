"""GraphBubbleUp exception family tests — engine never swallows."""
from __future__ import annotations

import pytest
from helpers import CounterState, make_ctx

from modex_graph import (
    Graph,
    GraphBubbleUp,
    GraphContext,
    GraphDrained,
    GraphEngine,
    GraphInterrupt,
    GraphNode,
    GraphRuntime,
    IntegratedInput,
    Node,
    NodeResult,
    ParentCommand,
)


class TestGraphBubbleUpFamily:
    """GraphBubbleUp exception family hierarchy."""

    def test_graphinterrupt_is_graphbubbleup(self) -> None:
        assert issubclass(GraphInterrupt, GraphBubbleUp)

    def test_graphdrained_is_graphbubbleup(self) -> None:
        assert issubclass(GraphDrained, GraphBubbleUp)

    def test_parentcommand_is_graphbubbleup(self) -> None:
        assert issubclass(ParentCommand, GraphBubbleUp)

    def test_graphinterrupt_carries_value(self) -> None:
        exc = GraphInterrupt(value={"key": "data"})
        assert exc.value == {"key": "data"}

    def test_graphinterrupt_carries_node_name(self) -> None:
        exc = GraphInterrupt(value="x", node_name="tool_node")
        assert exc.node_name == "tool_node"


class TestEngineDoesNotSwallow:
    """The engine NEVER catches and swallows GraphBubbleUp exceptions."""

    async def test_engine_propagates_graphinterrupt(self) -> None:
        class InterruptNode(Node[CounterState]):
            def execute(self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput) -> NodeResult:
                ctx.interrupt({"approval": "needed"})
                return NodeResult()

        g: Graph[CounterState] = Graph()
        g.add_node("n", InterruptNode())
        g.add_edge(GraphNode.START, "n")
        g.add_edge("n", GraphNode.END)
        compiled = g.compile()
        ctx = make_ctx(CounterState())
        with pytest.raises(GraphInterrupt) as exc_info:
            await GraphEngine(compiled).run_async(ctx)
        assert exc_info.value.value == {"approval": "needed"}

    async def test_engine_propagates_graphdrained(self) -> None:
        class DrainNode(Node[CounterState]):
            def execute(self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput) -> NodeResult:
                raise GraphDrained()

        g: Graph[CounterState] = Graph()
        g.add_node("n", DrainNode())
        g.add_edge(GraphNode.START, "n")
        g.add_edge("n", GraphNode.END)
        compiled = g.compile()
        ctx = make_ctx(CounterState())
        with pytest.raises(GraphDrained):
            await GraphEngine(compiled).run_async(ctx)

    async def test_engine_propagates_parentcommand(self) -> None:
        class ParentCmdNode(Node[CounterState]):
            def execute(self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput) -> NodeResult:
                raise ParentCommand("goto_parent")

        g: Graph[CounterState] = Graph()
        g.add_node("n", ParentCmdNode())
        g.add_edge(GraphNode.START, "n")
        g.add_edge("n", GraphNode.END)
        compiled = g.compile()
        ctx = make_ctx(CounterState())
        with pytest.raises(ParentCommand):
            await GraphEngine(compiled).run_async(ctx)

    async def test_engine_propagates_from_before_node(self) -> None:
        """GraphBubbleUp raised in runtime.before_node propagates."""

        class InterruptRuntime(GraphRuntime):
            async def before_node(self, ctx: GraphContext[CounterState], node_name: str) -> None:
                raise GraphInterrupt(value="from_before_node")

        class NoOpNode(Node[CounterState]):
            def execute(self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput) -> NodeResult:
                self.deliver(None, None, ctx)
                return NodeResult()

        g: Graph[CounterState] = Graph()
        g.add_node("n", NoOpNode())
        g.add_edge(GraphNode.START, "n")
        g.add_edge("n", GraphNode.END)
        compiled = g.compile()
        ctx = GraphContext(
            state=CounterState(),
            runtime=InterruptRuntime(),
        )
        with pytest.raises(GraphInterrupt) as exc_info:
            await GraphEngine(compiled).run_async(ctx)
        assert exc_info.value.value == "from_before_node"

    async def test_engine_propagates_from_after_node(self) -> None:
        """GraphBubbleUp raised in runtime.after_node propagates."""

        class DrainRuntime(GraphRuntime):
            async def after_node(
                self, ctx: GraphContext[CounterState], node_name: str, result: NodeResult
            ) -> None:
                raise GraphDrained()

        class NoOpNode(Node[CounterState]):
            def execute(self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput) -> NodeResult:
                self.deliver(None, None, ctx)
                return NodeResult()

        g: Graph[CounterState] = Graph()
        g.add_node("n", NoOpNode())
        g.add_edge(GraphNode.START, "n")
        g.add_edge("n", GraphNode.END)
        compiled = g.compile()
        ctx = GraphContext(
            state=CounterState(),
            runtime=DrainRuntime(),
        )
        with pytest.raises(GraphDrained):
            await GraphEngine(compiled).run_async(ctx)


class TestCtxInterrupt:
    """ctx.interrupt(value) raises GraphInterrupt."""

    def test_interrupt_is_no_return(self) -> None:
        ctx = make_ctx(CounterState())
        with pytest.raises(GraphInterrupt) as exc_info:
            ctx.interrupt("test_value")
        assert exc_info.value.value == "test_value"

    def test_interrupt_default_value(self) -> None:
        ctx = make_ctx(CounterState())
        with pytest.raises(GraphInterrupt) as exc_info:
            ctx.interrupt()
        assert exc_info.value.value is None
