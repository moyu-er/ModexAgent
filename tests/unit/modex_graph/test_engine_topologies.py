"""Engine topology tests: linear, conditional, loop, interrupt, sync/async/mixed."""
from __future__ import annotations

import pytest
from helpers import (
    AddNode,
    AsyncAddNode,
    CounterState,
    InterruptNode,
    make_ctx,
)

from modex_graph import (
    Command,
    Graph,
    GraphContext,
    GraphEngine,
    GraphInterrupt,
    GraphNode,
    GraphRecursionError,
    Node,
    NodeResult,
)


class TestLinearChain:
    """Linear chain: START → A → B → END."""

    async def test_linear_chain_executes_all_nodes(self) -> None:
        g: Graph[CounterState] = Graph()
        g.add_node("a", AddNode(amount=1))
        g.add_node("b", AddNode(amount=2))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", "b", reason=None)
        g.add_edge("b", GraphNode.END, reason=None)

        compiled = g.compile()
        ctx = make_ctx(CounterState(count=0))
        engine = GraphEngine(compiled)
        result = await engine.run_async(ctx)
        assert result.count == 3

    async def test_linear_chain_sync_run(self) -> None:
        g: Graph[CounterState] = Graph()
        g.add_node("a", AddNode(amount=5))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", GraphNode.END, reason=None)
        compiled = g.compile()
        ctx = make_ctx(CounterState(count=0))
        engine = GraphEngine(compiled)
        result = engine.run(ctx)
        assert result.count == 5


class TestTransitionBranch:
    """Transition-based branch: NodeResult(transition=...) + static edges.

    Replaces the former route_fn conditional-edge mechanism. The node inspects
    state and returns a transition key; the graph topology (declared via
    `add_edge(source, target, reason=key)`) routes to the matching branch.
    """

    async def test_transition_direct_routes_to_matched_edge(self) -> None:
        """Node returns transition="high"/"low"; static edge routes accordingly."""
        g: Graph[CounterState] = Graph()

        class DecideNode(Node[CounterState]):
            def execute(self, ctx: GraphContext[CounterState]) -> NodeResult:
                return NodeResult(
                    transition="high" if ctx.state.count > 5 else "low"
                )

        g.add_node("decide", DecideNode())
        g.add_node("high", AddNode(amount=10))
        g.add_node("low", AddNode(amount=1))
        g.add_edge(GraphNode.START, "decide")
        g.add_edge("decide", "high", reason="high")
        g.add_edge("decide", "low", reason="low")
        g.add_edge("high", GraphNode.END)
        g.add_edge("low", GraphNode.END)

        compiled = g.compile()
        ctx = make_ctx(CounterState(count=0))
        result = await GraphEngine(compiled).run_async(ctx)
        # count starts at 0, decide returns "low", low adds 1 → 1
        assert result.count == 1

    async def test_transition_key_mapped_routes_via_static_edge(self) -> None:
        """Transition key "A"/"B" routes to path_a/path_b via static edges.

        This replaces the former destinations={"A": "path_a"} key-mapping mode:
        the mapping is now declared in the graph topology via `reason=`,
        decoupling routing logic from node names.
        """
        g: Graph[CounterState] = Graph()

        class DecideNode(Node[CounterState]):
            def execute(self, ctx: GraphContext[CounterState]) -> NodeResult:
                return NodeResult(
                    transition="A" if ctx.state.count == 0 else "B"
                )

        g.add_node("decide", DecideNode())
        g.add_node("path_a", AddNode(amount=100))
        g.add_node("path_b", AddNode(amount=200))
        g.add_edge(GraphNode.START, "decide")
        g.add_edge("decide", "path_a", reason="A")
        g.add_edge("decide", "path_b", reason="B")
        g.add_edge("path_a", GraphNode.END)
        g.add_edge("path_b", GraphNode.END)

        compiled = g.compile()
        ctx = make_ctx(CounterState(count=0))
        result = await GraphEngine(compiled).run_async(ctx)
        assert result.count == 100

    async def test_transition_routes_to_end(self) -> None:
        """A transition can route directly to GraphNode.END via a static edge."""
        g: Graph[CounterState] = Graph()

        class DecideNode(Node[CounterState]):
            def execute(self, ctx: GraphContext[CounterState]) -> NodeResult:
                return NodeResult(transition="done")

        g.add_node("decide", DecideNode())
        g.add_edge(GraphNode.START, "decide")
        g.add_edge("decide", GraphNode.END, reason="done")
        compiled = g.compile()
        ctx = make_ctx(CounterState(count=0))
        result = await GraphEngine(compiled).run_async(ctx)
        assert result.count == 0


class TestLoopWithCycleGuard:
    """Loop with cycle guard: A→B→A until count exceeds threshold, then →END."""

    async def test_loop_exits_via_transition(self) -> None:
        class LoopNode(Node[CounterState]):
            def execute(self, ctx: GraphContext[CounterState]) -> NodeResult:
                ctx.state.count += 1
                if ctx.state.count >= 5:
                    return NodeResult(transition="done")
                return NodeResult(transition="loop")

        g: Graph[CounterState] = Graph()
        g.add_node("loop", LoopNode())
        g.add_edge(GraphNode.START, "loop")
        g.add_edge("loop", "loop", reason="loop")
        g.add_edge("loop", GraphNode.END, reason="done")
        compiled = g.compile(max_iterations=100)

        ctx = make_ctx(CounterState(count=0))
        result = await GraphEngine(compiled).run_async(ctx)
        assert result.count == 5

    async def test_max_iterations_raises_recursion_error(self) -> None:
        class InfiniteNode(Node[CounterState]):
            def execute(self, ctx: GraphContext[CounterState]) -> NodeResult:
                ctx.state.count += 1
                return NodeResult(transition="loop")

        g: Graph[CounterState] = Graph()
        g.add_node("inf", InfiniteNode())
        g.add_edge(GraphNode.START, "inf")
        g.add_edge("inf", "inf", reason="loop")
        compiled = g.compile(max_iterations=5)

        ctx = make_ctx(CounterState(count=0))
        with pytest.raises(GraphRecursionError, match="max_iterations=5"):
            await GraphEngine(compiled).run_async(ctx)


class TestHitlInterruptResume:
    """HITL interrupt + resume: ctx.interrupt(value) raises GraphInterrupt."""

    async def test_interrupt_raises_graphinterrupt(self) -> None:
        g: Graph[CounterState] = Graph()
        g.add_node("interrupt", InterruptNode(value="approval_needed"))
        g.add_edge(GraphNode.START, "interrupt")
        g.add_edge("interrupt", GraphNode.END)
        compiled = g.compile()

        ctx = make_ctx(CounterState(count=0))
        with pytest.raises(GraphInterrupt) as exc_info:
            await GraphEngine(compiled).run_async(ctx)
        assert exc_info.value.value == "approval_needed"

    async def test_interrupt_persists_prior_state_mutations(self) -> None:
        """Suspend-without-re-execution: mutations before interrupt persist."""

        class MutateThenInterrupt(Node[CounterState]):
            def execute(self, ctx: GraphContext[CounterState]) -> NodeResult:
                ctx.state.count += 42
                ctx.interrupt("paused")
                return NodeResult()

        g: Graph[CounterState] = Graph()
        g.add_node("n", MutateThenInterrupt())
        g.add_edge(GraphNode.START, "n")
        g.add_edge("n", GraphNode.END)
        compiled = g.compile()

        ctx = make_ctx(CounterState(count=0))
        with pytest.raises(GraphInterrupt):
            await GraphEngine(compiled).run_async(ctx)
        # The mutation persisted across the interrupt.
        assert ctx.state.count == 42

    async def test_resume_re_enters_from_entry_node(self) -> None:
        """Resume: engine starts from entry_node; topology detects suspended state."""

        class StartNode(Node[CounterState]):
            """Detects suspended state: if count > 0, route to 'after_resume'."""

            def execute(self, ctx: GraphContext[CounterState]) -> NodeResult:
                if ctx.state.count > 0:
                    return NodeResult(transition="resumed")
                ctx.state.count += 1
                return NodeResult(transition="initial")

        g: Graph[CounterState] = Graph()
        g.add_node("start", StartNode())
        g.add_node("interrupt", InterruptNode(value="pause"))
        g.add_node("after_resume", AddNode(amount=100))
        g.add_edge(GraphNode.START, "start")
        g.add_edge("start", "interrupt", reason="initial")
        g.add_edge("start", "after_resume", reason="resumed")
        g.add_edge("interrupt", GraphNode.END)
        g.add_edge("after_resume", GraphNode.END)
        compiled = g.compile()

        # First run: start → interrupt → raises.
        ctx = make_ctx(CounterState(count=0))
        with pytest.raises(GraphInterrupt):
            await GraphEngine(compiled).run_async(ctx)
        assert ctx.state.count == 1

        # Resume: same engine, same entry_node. start detects count > 0 → after_resume.
        result = await GraphEngine(compiled).run_async(ctx)
        assert result.count == 101

    async def test_resume_routes_via_resume_target_channel(self) -> None:
        """Resume routing via state.resume_target + Command(goto=...)."""

        class EntryNode(Node[CounterState]):
            def execute(self, ctx: GraphContext[CounterState]) -> NodeResult:
                if ctx.state.resume_target is not None:
                    target = ctx.state.resume_target
                    ctx.state.resume_target = None
                    return NodeResult(command=Command(goto=target))
                ctx.state.count += 1
                return NodeResult(transition="initial")

        class SuspendNode(Node[CounterState]):
            def execute(self, ctx: GraphContext[CounterState]) -> NodeResult:
                ctx.state.resume_target = "after_resume"
                ctx.interrupt("paused")
                return NodeResult()

        g: Graph[CounterState] = Graph()
        g.add_node("start", EntryNode())
        g.add_node("suspend", SuspendNode())
        g.add_node("after_resume", AddNode(amount=100))
        g.add_edge(GraphNode.START, "start")
        g.add_edge("start", "suspend", reason="initial")
        g.add_edge("suspend", GraphNode.END)
        g.add_edge("after_resume", GraphNode.END)
        compiled = g.compile()

        ctx = make_ctx(CounterState(count=0))
        with pytest.raises(GraphInterrupt):
            await GraphEngine(compiled).run_async(ctx)
        assert ctx.state.count == 1
        assert ctx.state.resume_target == "after_resume"

        result = await GraphEngine(compiled).run_async(ctx)
        assert result.count == 101
        assert result.resume_target is None


class TestSyncAsyncMixed:
    """Sync-only / async-only / mixed sync+async nodes in one graph."""

    async def test_sync_only_node(self) -> None:
        g: Graph[CounterState] = Graph()
        g.add_node("a", AddNode(amount=7))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", GraphNode.END)
        compiled = g.compile()
        ctx = make_ctx(CounterState(count=0))
        result = await GraphEngine(compiled).run_async(ctx)
        assert result.count == 7

    async def test_async_only_node(self) -> None:
        g: Graph[CounterState] = Graph()
        g.add_node("a", AsyncAddNode(amount=8))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", GraphNode.END)
        compiled = g.compile()
        ctx = make_ctx(CounterState(count=0))
        result = await GraphEngine(compiled).run_async(ctx)
        assert result.count == 8

    async def test_mixed_sync_and_async_nodes(self) -> None:
        g: Graph[CounterState] = Graph()
        g.add_node("sync_a", AddNode(amount=1))
        g.add_node("async_b", AsyncAddNode(amount=2))
        g.add_node("sync_c", AddNode(amount=3))
        g.add_edge(GraphNode.START, "sync_a")
        g.add_edge("sync_a", "async_b", reason=None)
        g.add_edge("async_b", "sync_c", reason=None)
        g.add_edge("sync_c", GraphNode.END)
        compiled = g.compile()
        ctx = make_ctx(CounterState(count=0))
        result = await GraphEngine(compiled).run_async(ctx)
        assert result.count == 6

    async def test_sync_run_with_async_nodes(self) -> None:
        """Sync run() works with async nodes via asyncio.run."""
        g: Graph[CounterState] = Graph()
        g.add_node("a", AsyncAddNode(amount=9))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", GraphNode.END)
        compiled = g.compile()
        ctx = make_ctx(CounterState(count=0))
        result = engine_run_helper(compiled, ctx)
        assert result.count == 9


def engine_run_helper(compiled, ctx):
    """Helper to run engine synchronously."""
    return GraphEngine(compiled).run(ctx)
