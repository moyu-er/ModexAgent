"""Tests for GraphEngine."""
import pytest
from modex_agent.core.graph.engine import GraphEngine
from modex_agent.core.graph.graph import Graph
from modex_agent.core.graph.node import Node, NodeTransition
from modex_agent.core.graph.constants import GraphNode
from modex_agent.core.graph.interrupt import GraphInterrupt
from modex_agent.runtime.enums import AgentKind, TurnCustomKey, TurnPhase
from modex_agent.runtime.models import TurnIdentity, TurnStateBase
from modex_agent.runtime.services import AgentRuntime, AgentRuntimeServices
from modex_agent.core.session_id import SessionInfo


class _MinimalRuntime:
    def __init__(self) -> None:
        self.state = TurnStateBase(
            identity=TurnIdentity(agent_id="test", session=SessionInfo.from_str("s1"), turn_id="t1"),
            agent_kind=AgentKind.REACT,
            phase=TurnPhase.RUNNING,
        )


class _TrackedNode(Node):
    def __init__(self, name: str, next_target: str, next_reason: str,
                 side_effect=None):
        super().__init__(name)
        self.next_transition = NodeTransition(next_target, next_reason)
        self.side_effect = side_effect
        self.call_count = 0

    async def execute(self, ctx):
        self.call_count += 1
        if self.side_effect:
            self.side_effect(ctx)
        return self.next_transition


class _Ctx:
    def __init__(self):
        self.metadata = {}
        self.runtime = _MinimalRuntime()


class TestGraphEngine:
    @pytest.mark.asyncio
    async def test_single_node_to_end(self):
        g = Graph()
        node = _TrackedNode("start", GraphNode.END, "done")
        g.add_node(node)

        ctx = _Ctx()
        result = await GraphEngine(g).run(ctx)
        assert node.call_count == 1

    @pytest.mark.asyncio
    async def test_two_node_chain(self):
        g = Graph()
        n1 = _TrackedNode("start", "n2", "go")
        n2 = _TrackedNode("n2", GraphNode.END, "done")
        g.add_node(n1)
        g.add_node(n2)
        g.add_edge("start", "n2", reason="go")

        ctx = _Ctx()
        result = await GraphEngine(g).run(ctx)
        assert n1.call_count == 1
        assert n2.call_count == 1

    @pytest.mark.asyncio
    async def test_build_result_reads_typed_state(self):
        g = Graph()
        def side(ctx):
            ctx.runtime.state.custom[TurnCustomKey.GRAPH_RESULT] = 42
        node = _TrackedNode("start", GraphNode.END, "done", side_effect=side)
        g.add_node(node)

        ctx = _Ctx()
        result = await GraphEngine(g).run(ctx)
        assert result == 42

    @pytest.mark.asyncio
    async def test_build_result_overridable(self):
        class MyEngine(GraphEngine):
            def build_result(self, ctx):
                return "custom"

        g = Graph()
        g.add_node(_TrackedNode("start", GraphNode.END, "done"))
        ctx = _Ctx()
        result = await MyEngine(g).run(ctx)
        assert result == "custom"

    @pytest.mark.asyncio
    async def test_graph_interrupt_propagates(self):
        class _RaiseNode(Node):
            def __init__(self):
                super().__init__("start")
            async def execute(self, ctx):
                raise GraphInterrupt(value="paused", node_name="start", iteration=1)

        g = Graph()
        g.add_node(_RaiseNode())
        ctx = _Ctx()

        with pytest.raises(GraphInterrupt) as exc_info:
            await GraphEngine(g).run(ctx)
        assert exc_info.value.value == "paused"
