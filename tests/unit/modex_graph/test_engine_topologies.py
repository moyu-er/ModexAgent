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
    Graph,
    GraphContext,
    GraphEngine,
    GraphInterrupt,
    GraphNode,
    GraphRecursionError,
    IntegratedInput,
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
        g.add_edge("a", "b")
        g.add_edge("b", GraphNode.END)

        compiled = g.compile()
        ctx = make_ctx(CounterState(count=0))
        engine = GraphEngine(compiled)
        result = await engine.run_async(ctx)
        assert result.count == 3

    async def test_linear_chain_sync_run(self) -> None:
        g: Graph[CounterState] = Graph()
        g.add_node("a", AddNode(amount=5))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", GraphNode.END)
        compiled = g.compile()
        ctx = make_ctx(CounterState(count=0))
        engine = GraphEngine(compiled)
        result = engine.run(ctx)
        assert result.count == 5


class TestLoopWithCycleGuard:
    """Loop with cycle guard: A→B→A until count exceeds threshold, then →END."""

    async def test_loop_exits_via_deliver(self) -> None:
        class LoopNode(Node[CounterState]):
            def execute(self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput) -> NodeResult:
                ctx.state.count += 1
                if ctx.state.count >= 5:
                    self.deliver(None, GraphNode.END, ctx)
                else:
                    self.deliver(None, "loop", ctx)
                return NodeResult()

        g: Graph[CounterState] = Graph()
        g.add_node("loop", LoopNode())
        g.add_edge(GraphNode.START, "loop")
        g.add_edge("loop", "loop")
        g.add_edge("loop", GraphNode.END)
        compiled = g.compile(max_iterations=100)

        ctx = make_ctx(CounterState(count=0))
        result = await GraphEngine(compiled).run_async(ctx)
        assert result.count == 5

    async def test_max_iterations_raises_recursion_error(self) -> None:
        class InfiniteNode(Node[CounterState]):
            def execute(self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput) -> NodeResult:
                ctx.state.count += 1
                self.deliver(None, "inf", ctx)
                return NodeResult()

        g: Graph[CounterState] = Graph()
        g.add_node("inf", InfiniteNode())
        g.add_edge(GraphNode.START, "inf")
        g.add_edge("inf", "inf")
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
            def execute(self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput) -> NodeResult:
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

            def execute(self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput) -> NodeResult:
                if ctx.state.count > 0:
                    self.deliver(None, "after_resume", ctx)
                else:
                    ctx.state.count += 1
                    self.deliver(None, "interrupt", ctx)
                return NodeResult()

        g: Graph[CounterState] = Graph()
        g.add_node("start", StartNode())
        g.add_node("interrupt", InterruptNode(value="pause"))
        g.add_node("after_resume", AddNode(amount=100))
        g.add_edge(GraphNode.START, "start")
        g.add_edge("start", "interrupt")
        g.add_edge("start", "after_resume")
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
        """Resume routing via state.resume_target + deliver()."""

        class EntryNode(Node[CounterState]):
            def execute(self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput) -> NodeResult:
                if ctx.state.resume_target is not None:
                    target = ctx.state.resume_target
                    ctx.state.resume_target = None
                    self.deliver(None, target, ctx)
                else:
                    ctx.state.count += 1
                    self.deliver(None, "suspend", ctx)
                return NodeResult()

        class SuspendNode(Node[CounterState]):
            def execute(self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput) -> NodeResult:
                ctx.state.resume_target = "after_resume"
                ctx.interrupt("paused")
                return NodeResult()

        g: Graph[CounterState] = Graph()
        g.add_node("start", EntryNode())
        g.add_node("suspend", SuspendNode())
        g.add_node("after_resume", AddNode(amount=100))
        g.add_edge(GraphNode.START, "start")
        g.add_edge("start", "suspend")
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
        g.add_edge("sync_a", "async_b")
        g.add_edge("async_b", "sync_c")
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
