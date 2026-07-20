"""Routing tests: Command.goto str / list[str] / list[Task] + default edge."""
from __future__ import annotations

from typing import Any

from helpers import CounterState, make_ctx

from modex_graph import (
    Command,
    Graph,
    GraphContext,
    GraphEngine,
    GraphNode,
    Node,
    NodeResult,
    Task,
)


class _RecordNameNode(Node[CounterState]):
    """Records its name into state.messages."""

    def execute(self, ctx: GraphContext[CounterState]) -> NodeResult:
        ctx.state.messages = ctx.state.messages + [self.name]
        return NodeResult()


class _CommandNode(Node[CounterState]):
    """Returns a Command(goto=...)."""

    def __init__(self, goto: Any) -> None:
        self.goto = goto

    def execute(self, ctx: GraphContext[CounterState]) -> NodeResult:
        return NodeResult(command=Command(goto=self.goto))


class _StateUpdateNode(Node[CounterState]):
    """Returns NodeResult(state_update={"messages": [label]})."""

    def __init__(self, label: str) -> None:
        self.label = label

    def execute(self, ctx: GraphContext[CounterState]) -> NodeResult:
        return NodeResult(state_update={"messages": [self.label]})


class TestCommandGotoStr:
    """Command(goto="node") — dynamic routing to one node."""

    async def test_goto_str_routes_to_target(self) -> None:
        g: Graph[CounterState] = Graph()
        g.add_node("start", _CommandNode(goto="target"))
        g.add_node("target", _RecordNameNode())
        g.add_edge(GraphNode.START, "start")
        g.add_edge("target", GraphNode.END)
        compiled = g.compile()
        ctx = make_ctx(CounterState())
        result = await GraphEngine(compiled).run_async(ctx)
        assert result.messages == ["target"]

    async def test_goto_str_overrides_transition(self) -> None:
        """Command.goto has higher priority than transition."""

        class BothNode(Node[CounterState]):
            def execute(self, ctx: GraphContext[CounterState]) -> NodeResult:
                return NodeResult(
                    transition="ignored", command=Command(goto="real_target")
                )

        g: Graph[CounterState] = Graph()
        g.add_node("start", BothNode())
        g.add_node("wrong", _RecordNameNode())
        g.add_node("real_target", _RecordNameNode())
        g.add_edge(GraphNode.START, "start")
        g.add_edge("start", "wrong", reason="ignored")
        g.add_edge("real_target", GraphNode.END)
        g.add_edge("wrong", GraphNode.END)
        compiled = g.compile()
        ctx = make_ctx(CounterState())
        result = await GraphEngine(compiled).run_async(ctx)
        # Command.goto overrode transition → "real_target" executed, "wrong" did not.
        assert result.messages == ["real_target"]


class TestCommandGotoListStr:
    """Command(goto=["a", "b", "c"]) — sequential multi-target."""

    async def test_list_str_visits_all_in_order(self) -> None:
        g: Graph[CounterState] = Graph()
        g.add_node("start", _CommandNode(goto=["a", "b", "c"]))
        g.add_node("a", _RecordNameNode())
        g.add_node("b", _RecordNameNode())
        g.add_node("c", _RecordNameNode())
        g.add_edge(GraphNode.START, "start")
        g.add_edge("c", GraphNode.END)
        compiled = g.compile()
        ctx = make_ctx(CounterState())
        result = await GraphEngine(compiled).run_async(ctx)
        assert result.messages == ["a", "b", "c"]

    async def test_list_str_after_last_uses_normal_routing(self) -> None:
        """After the last node in list[str], normal routing applies from that node."""
        g: Graph[CounterState] = Graph()
        g.add_node("start", _CommandNode(goto=["a"]))
        g.add_node("a", _RecordNameNode())
        g.add_node("b", _RecordNameNode())
        g.add_edge(GraphNode.START, "start")
        g.add_edge("a", "b", reason=None)  # default edge from "a"
        g.add_edge("b", GraphNode.END)
        compiled = g.compile()
        ctx = make_ctx(CounterState())
        result = await GraphEngine(compiled).run_async(ctx)
        assert result.messages == ["a", "b"]


class TestCommandGotoListTask:
    """Command(goto=[Task(node, state)]) — sequential fan-out with independent state."""

    async def test_list_task_executes_all_with_independent_state(self) -> None:
        """Each Task carries independent state; imperative mutations don't propagate."""

        class CounterNode(Node[CounterState]):
            """Increments count on its (possibly forked) state, records name."""

            def execute(self, ctx: GraphContext[CounterState]) -> NodeResult:
                ctx.state.count += 1
                # Use state_update to merge name back to PARENT state.
                return NodeResult(state_update={"messages": [self.name]})

        g: Graph[CounterState] = Graph()
        g.add_node(
            "start",
            _CommandNode(
                goto=[
                    Task(node="worker", state=CounterState(count=0, name="")),
                    Task(node="worker", state=CounterState(count=0, name="")),
                    Task(node="worker", state=CounterState(count=0, name="")),
                ]
            ),
        )
        g.add_node("worker", CounterNode())
        g.add_edge(GraphNode.START, "start")
        compiled = g.compile()
        ctx = make_ctx(CounterState(count=0, name=""))
        result = await GraphEngine(compiled).run_async(ctx)
        # Each worker ran once. state_update merged their names to parent.
        assert result.messages == ["worker", "worker", "worker"]
        # The workers' imperative count mutations did NOT propagate (independent state).
        # Only state_update merges back. CounterNode didn't use state_update for count.
        assert result.count == 0

    async def test_list_task_state_update_merges_to_parent(self) -> None:
        """state_update from tasks merges to parent via reducer channels."""

        class MergeNode(Node[CounterState]):
            def execute(self, ctx: GraphContext[CounterState]) -> NodeResult:
                return NodeResult(state_update={"messages": [self.name]})

        g: Graph[CounterState] = Graph()
        g.add_node(
            "start",
            _CommandNode(
                goto=[
                    Task(node="w1", state=CounterState()),
                    Task(node="w2", state=CounterState()),
                ]
            ),
        )
        g.add_node("w1", MergeNode())
        g.add_node("w2", MergeNode())
        g.add_edge(GraphNode.START, "start")
        compiled = g.compile()
        ctx = make_ctx(CounterState())
        result = await GraphEngine(compiled).run_async(ctx)
        # ReducerChannel folded ["w1"] + ["w2"] → ["w1", "w2"]
        assert result.messages == ["w1", "w2"]


class TestDefaultEdgeFallback:
    """Default edge (reason=None) is the lowest-priority routing mechanism."""

    async def test_default_edge_used_when_no_transition(self) -> None:
        g: Graph[CounterState] = Graph()
        g.add_node("start", _RecordNameNode())
        g.add_node("next", _RecordNameNode())
        g.add_edge(GraphNode.START, "start")
        g.add_edge("start", "next", reason=None)
        g.add_edge("next", GraphNode.END)
        compiled = g.compile()
        ctx = make_ctx(CounterState())
        result = await GraphEngine(compiled).run_async(ctx)
        assert result.messages == ["start", "next"]


class TestRoutingPriority:
    """Strict priority: Command.goto > transition > conditional > default."""

    async def test_transition_beats_conditional_and_default(self) -> None:
        class TransitionNode(Node[CounterState]):
            def execute(self, ctx: GraphContext[CounterState]) -> NodeResult:
                return NodeResult(transition="explicit")

        g: Graph[CounterState] = Graph()
        g.add_node("start", TransitionNode())
        g.add_node("explicit_target", _RecordNameNode())
        g.add_node("cond_target", _RecordNameNode())
        g.add_node("default_target", _RecordNameNode())
        g.add_edge(GraphNode.START, "start")
        g.add_edge("start", "explicit_target", reason="explicit")
        g.add_edge("start", "default_target", reason=None)

        def route(state: CounterState) -> str:
            return "cond_target"

        g.add_conditional_edges("start", route)
        g.add_edge("explicit_target", GraphNode.END)
        g.add_edge("cond_target", GraphNode.END)
        g.add_edge("default_target", GraphNode.END)
        compiled = g.compile()
        ctx = make_ctx(CounterState())
        result = await GraphEngine(compiled).run_async(ctx)
        # transition beat conditional + default
        assert result.messages == ["explicit_target"]
