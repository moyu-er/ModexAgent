"""Routing tests for ParallelScheduler (Task 04).

Covers:

- Deliver-based fan-out: A delivers to B and C → both instances created.
- Fan-out + fan-in end-to-end (A → [B, C] → D).
- `CompiledGraph.edges_from` returns all edges from a source.
"""

from __future__ import annotations

import asyncio

from helpers import CounterState, make_coordinator, make_runtime

from modex_graph import (
    Graph,
    GraphContext,
    GraphEngine,
    GraphNode,
    IntegratedInput,
    Node,
    NodeTrigger,
    ParallelScheduler,
    SchedulerKind,
)
from modex_graph.scheduler.bootstrap import BootstrapMode


def make_parallel_ctx(state: CounterState | None = None) -> GraphContext[CounterState]:
    return GraphContext(
        state=state if state is not None else CounterState(),
        runtime=make_runtime(),
        coordinator=make_coordinator(),
        scheduler_kind=SchedulerKind.PARALLEL,
    )


class FanOutDeliverNode(Node[CounterState]):
    """Increments count, delivers to multiple targets (fan-out via deliver)."""

    def __init__(self, amount: int, targets: list[str]) -> None:
        self.amount = amount
        self.targets = targets

    async def execute(
        self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput
    ) -> None:
        ctx.state.count += self.amount
        for target in self.targets:
            self.deliver(None, target, ctx)
        return None


class NoOpNode(Node[CounterState]):
    """Delivers to default target — no explicit routing."""

    async def execute(
        self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput
    ) -> None:
        self.deliver(None, None, ctx)
        return None


class AddAndDispatchNode(Node[CounterState]):
    """Increments count, delivers to a target."""

    def __init__(self, amount: int, target: str) -> None:
        self.amount = amount
        self.target = target

    async def execute(
        self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput
    ) -> None:
        ctx.state.count += self.amount
        self.deliver(None, self.target, ctx)
        return None


class AsyncAddAndDispatchNode(Node[CounterState]):
    def __init__(self, amount: int, target: str) -> None:
        self.amount = amount
        self.target = target

    async def execute(
        self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput
    ) -> None:
        await asyncio.sleep(0)
        ctx.state.count += self.amount
        self.deliver(None, self.target, ctx)
        return None


# ── CompiledGraph edge lookup ────────────────────────────────────────────


class TestCompiledGraphEdgeLookup:
    def test_edges_from_returns_all(self) -> None:
        g: Graph[CounterState] = Graph()
        g.add_node("a", NoOpNode())
        g.add_node("b", NoOpNode())
        g.add_node("c", NoOpNode())
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", "b")
        g.add_edge("a", "c")
        compiled = g.compile()

        targets = [e.target for e in compiled.edges_from("a")]
        assert targets == ["b", "c"]

    def test_edges_from_empty_when_none(self) -> None:
        g: Graph[CounterState] = Graph()
        g.add_node("a", NoOpNode())
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", GraphNode.END)
        compiled = g.compile()

        assert compiled.edges_from("a") == [compiled.edges[1]]


# ── Deliver-based fan-out ──────────────────────────────────────────────────


class TestDeliverFanOut:
    """A delivers to B and C → both instances created (fan-out via deliver)."""

    async def test_deliver_fan_out_both_dispatched(self) -> None:
        g: Graph[CounterState] = Graph()
        g.add_node("a", FanOutDeliverNode(amount=1, targets=["b", "c"]))
        g.add_node("b", AddAndDispatchNode(amount=10, target=GraphNode.END))
        g.add_node("c", AddAndDispatchNode(amount=100, target=GraphNode.END))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", "b")
        g.add_edge("a", "c")
        g.add_edge("b", GraphNode.END)
        g.add_edge("c", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = make_parallel_ctx(CounterState(count=0))
        result = await GraphEngine(compiled).run_async(ctx, mode=BootstrapMode.FRESH)
        assert result.count == 111

    async def test_deliver_fan_out_creates_two_instances(self) -> None:
        g: Graph[CounterState] = Graph()
        g.add_node("a", FanOutDeliverNode(amount=1, targets=["b", "c"]))
        g.add_node("b", AddAndDispatchNode(amount=10, target=GraphNode.END))
        g.add_node("c", AddAndDispatchNode(amount=100, target=GraphNode.END))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", "b")
        g.add_edge("a", "c")
        g.add_edge("b", GraphNode.END)
        g.add_edge("c", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = make_parallel_ctx(CounterState(count=0))
        engine = GraphEngine(compiled)
        await engine.run_async(ctx, mode=BootstrapMode.FRESH)

        scheduler = engine._scheduler
        assert isinstance(scheduler, ParallelScheduler)
        b_instances = [i for i in scheduler._instances.values() if i.node_name == "b"]
        c_instances = [i for i in scheduler._instances.values() if i.node_name == "c"]
        assert len(b_instances) == 1
        assert len(c_instances) == 1


# ── Fan-out + fan-in end-to-end ────────────────────────────────────────────


class TestFanOutFanIn:
    """A → [B, C] → D → END via deliver-based fan-out."""

    async def test_fanout_fanin_completes(self) -> None:
        g: Graph[CounterState] = Graph()
        g.add_node("a", FanOutDeliverNode(amount=1, targets=["b", "c"]))
        g.add_node("b", AddAndDispatchNode(amount=10, target="d"))
        g.add_node("c", AddAndDispatchNode(amount=100, target="d"))
        g.add_node("d", AddAndDispatchNode(amount=1000, target=GraphNode.END))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", "b")
        g.add_edge("a", "c")
        g.add_edge("b", "d")
        g.add_edge("c", "d")
        g.add_edge("d", GraphNode.END)
        compiled = g.compile(
            scheduler=SchedulerKind.PARALLEL,
            default_trigger=NodeTrigger.ON_RECEIVE,
        )

        ctx = make_parallel_ctx(CounterState(count=0))
        result = await GraphEngine(compiled).run_async(ctx, mode=BootstrapMode.FRESH)
        assert result.count == 2111

    async def test_fanout_fanin_d_executes_twice(self) -> None:
        g: Graph[CounterState] = Graph()
        g.add_node("a", FanOutDeliverNode(amount=1, targets=["b", "c"]))
        g.add_node("b", AddAndDispatchNode(amount=10, target="d"))
        g.add_node("c", AddAndDispatchNode(amount=100, target="d"))
        g.add_node("d", AddAndDispatchNode(amount=1000, target=GraphNode.END))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", "b")
        g.add_edge("a", "c")
        g.add_edge("b", "d")
        g.add_edge("c", "d")
        g.add_edge("d", GraphNode.END)
        compiled = g.compile(
            scheduler=SchedulerKind.PARALLEL,
            default_trigger=NodeTrigger.ON_RECEIVE,
        )

        ctx = make_parallel_ctx(CounterState(count=0))
        engine = GraphEngine(compiled)
        await engine.run_async(ctx, mode=BootstrapMode.FRESH)

        scheduler = engine._scheduler
        assert isinstance(scheduler, ParallelScheduler)
        d_instances = [i for i in scheduler._instances.values() if i.node_name == "d"]
        assert len(d_instances) == 2

    async def test_async_branches_keep_their_dispatch_source(self) -> None:
        g: Graph[CounterState] = Graph()
        g.add_node("a", FanOutDeliverNode(amount=1, targets=["b", "c"]))
        g.add_node("b", AsyncAddAndDispatchNode(amount=10, target="d"))
        g.add_node("c", AsyncAddAndDispatchNode(amount=100, target="e"))
        g.add_node("d", AddAndDispatchNode(amount=1000, target=GraphNode.END))
        g.add_node("e", AddAndDispatchNode(amount=10000, target=GraphNode.END))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", "b")
        g.add_edge("a", "c")
        g.add_edge("b", "d")
        g.add_edge("c", "e")
        g.add_edge("d", GraphNode.END)
        g.add_edge("e", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        result = await GraphEngine(compiled).run_async(make_parallel_ctx(CounterState()), mode=BootstrapMode.FRESH)

        assert result.count == 11111
