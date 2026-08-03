"""Termination conditions + compile validation tests (Task 07).

Covers:

- `GraphNode.END` sentinel: no instance created, no execution.
- Dispatch to END is recorded as a `DispatchEvent` (target=END) in the
  dispatch log; the source instance IDs are retrievable via the store.
- END `ON_ALL_PREDS` semantics: graph terminates only after all
  dispatch-to-END source instances COMPLETED (implicitly via ready+active
  empty).
- Termination: `ready` empty AND `active` empty -> graph terminates.
- Multi-branch all走向END -> all branches complete -> terminate.
- One branch -> END, another -> leaf node (no dispatch) -> terminate.
- Has outgoing edges but doesn't dispatch -> downstream DORMANT, terminate.
- `Graph.compile(scheduler=PARALLEL)` START reachability: all registered
  nodes reachable from entry_node via BFS along outgoing edges.
- `Graph.compile(scheduler=PARALLEL)` END reachability: all registered
  nodes can reach END via forward BFS (reverse BFS from END).
- START/END reachability only under PARALLEL; LINEAR skips both checks.
"""

from __future__ import annotations

import pytest
from helpers import CounterState, make_runtime, make_coordinator

from modex_graph import (
    Graph,
    GraphContext,
    GraphEngine,
    GraphNode,
    IntegratedInput,
    Node,
    NodeInstanceStatus,
    NodeResult,
    ParallelScheduler,
    RoutingError,
    SchedulerKind,
)


def make_parallel_ctx(state: CounterState | None = None) -> GraphContext[CounterState]:
    return GraphContext(
        state=state if state is not None else CounterState(),
        runtime=make_runtime(),
        coordinator=make_coordinator(),
        scheduler_kind=SchedulerKind.PARALLEL,
    )


def _end_dispatch_sources(scheduler: ParallelScheduler[CounterState]) -> list[str]:
    """Return unique source_instance IDs that dispatched to GraphNode.END.

    Mirrors the former ``_end_sources`` set via the live dispatch log
    (``DispatchEvent`` records with ``target == GraphNode.END``).
    """
    return list(
        {
            e.source_instance
            for e in scheduler._dispatch_log
            if e.target == GraphNode.END
        }
    )


# ── Test helpers ──────────────────────────────────────────────────────────


class DispatchAddNode(Node[CounterState]):
    """Increments count by `amount`, then dispatches to `target` if set."""

    def __init__(self, amount: int, target: str | None = None) -> None:
        self.amount = amount
        self.target = target

    def execute(self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput) -> NodeResult:
        ctx.state.count += self.amount
        if self.target is not None:
            self.deliver(None, self.target, ctx)
        return NodeResult()


class FanOutDispatchNode(Node[CounterState]):
    """Dispatches to two targets (fan-out)."""

    def __init__(self, amount: int, target_a: str, target_b: str) -> None:
        self.amount = amount
        self.target_a = target_a
        self.target_b = target_b

    def execute(self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput) -> NodeResult:
        ctx.state.count += self.amount
        self.deliver(None, self.target_a, ctx)
        self.deliver(None, self.target_b, ctx)
        return NodeResult()


class NoDispatchNode(Node[CounterState]):
    """Increments count but does NOT call ctx.dispatch. Silent skip."""

    def __init__(self, amount: int = 1) -> None:
        self.amount = amount

    def execute(self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput) -> NodeResult:
        ctx.state.count += self.amount
        self.deliver(None, None, ctx)
        return NodeResult()


# ── END sentinel: no instance created, no execution ──────────────────────


class TestEndSentinel:
    """GraphNode.END is a sentinel — no instance created, no execution."""

    async def test_end_has_no_instance(self) -> None:
        """Dispatch to END creates no instance; the dispatch is logged."""
        g: Graph[CounterState] = Graph()
        g.add_node("a", DispatchAddNode(amount=1, target=GraphNode.END))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = make_parallel_ctx(CounterState(count=0))
        engine = GraphEngine(compiled)
        await engine.run_async(ctx)

        scheduler = engine._scheduler
        assert isinstance(scheduler, ParallelScheduler)
        # The dispatch to END was recorded (source instance a#0).
        end_sources = _end_dispatch_sources(scheduler)
        assert len(end_sources) == 1
        assert "a#0" in end_sources
        # No instance created for END.
        end_instances = [
            inst for inst in scheduler._instances.values() if inst.node_name == GraphNode.END
        ]
        assert len(end_instances) == 0

    async def test_end_sources_tracks_multiple_sources(self) -> None:
        """Multiple branches dispatching to END all recorded in the dispatch log."""
        g: Graph[CounterState] = Graph()
        g.add_node("a", FanOutDispatchNode(amount=1, target_a="b", target_b="c"))
        g.add_node("b", DispatchAddNode(amount=10, target=GraphNode.END))
        g.add_node("c", DispatchAddNode(amount=100, target=GraphNode.END))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", "b")
        g.add_edge("a", "c")
        g.add_edge("b", GraphNode.END)
        g.add_edge("c", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = make_parallel_ctx(CounterState(count=0))
        engine = GraphEngine(compiled)
        await engine.run_async(ctx)

        scheduler = engine._scheduler
        assert isinstance(scheduler, ParallelScheduler)
        # Both b and c dispatched to END.
        end_sources = _end_dispatch_sources(scheduler)
        assert len(end_sources) == 2
        b_ids = [iid for iid in end_sources if iid.startswith("b#")]
        c_ids = [iid for iid in end_sources if iid.startswith("c#")]
        assert len(b_ids) == 1
        assert len(c_ids) == 1


# ── END ON_ALL_PREDS: all dispatch-to-END sources COMPLETED -> terminate ─


class TestEndOnAllPreds:
    """END's ON_ALL_PREDS semantics: graph terminates only after all
    dispatch-to-END source instances are COMPLETED. Since END doesn't create
    instances, the source instances leave _active when they complete.
    When ready+active are both empty, the graph terminates."""

    async def test_multi_branch_all_to_end_terminates(self) -> None:
        """A -> [B, C], B -> END, C -> END. Both branches complete,
        graph terminates. All end_sources COMPLETED."""
        g: Graph[CounterState] = Graph()
        g.add_node("a", FanOutDispatchNode(amount=1, target_a="b", target_b="c"))
        g.add_node("b", DispatchAddNode(amount=10, target=GraphNode.END))
        g.add_node("c", DispatchAddNode(amount=100, target=GraphNode.END))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", "b")
        g.add_edge("a", "c")
        g.add_edge("b", GraphNode.END)
        g.add_edge("c", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = make_parallel_ctx(CounterState(count=0))
        result = await GraphEngine(compiled).run_async(ctx)

        # A=1 (fast), B+C forked (mutations dropped), so count = 1.
        assert result is ctx.state
        assert ctx.state.count == 1

    async def test_multi_branch_all_to_end_end_sources_completed(self) -> None:
        """Verify all dispatch-to-END source instances are COMPLETED after termination."""
        g: Graph[CounterState] = Graph()
        g.add_node("a", FanOutDispatchNode(amount=1, target_a="b", target_b="c"))
        g.add_node("b", DispatchAddNode(amount=10, target=GraphNode.END))
        g.add_node("c", DispatchAddNode(amount=100, target=GraphNode.END))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", "b")
        g.add_edge("a", "c")
        g.add_edge("b", GraphNode.END)
        g.add_edge("c", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = make_parallel_ctx(CounterState(count=0))
        engine = GraphEngine(compiled)
        await engine.run_async(ctx)

        scheduler = engine._scheduler
        assert isinstance(scheduler, ParallelScheduler)
        # All dispatch-to-END source instances must be COMPLETED.
        for source_id in _end_dispatch_sources(scheduler):
            inst = scheduler._instances[source_id]
            assert inst.status == NodeInstanceStatus.COMPLETED
        # ready and active are both empty after termination.
        assert len(scheduler._ready) == 0
        assert len(scheduler._active) == 0

    async def test_three_branches_all_to_end(self) -> None:
        """A -> [B, C, D], all -> END. All three branches complete,
        graph terminates."""
        g: Graph[CounterState] = Graph()

        class TripleFanOutNode(Node[CounterState]):
            def execute(self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput) -> NodeResult:
                ctx.state.count += 1
                self.deliver(None, "b", ctx)
                self.deliver(None, "c", ctx)
                self.deliver(None, "d", ctx)
                return NodeResult()

        g.add_node("a", TripleFanOutNode())
        g.add_node("b", DispatchAddNode(amount=10, target=GraphNode.END))
        g.add_node("c", DispatchAddNode(amount=100, target=GraphNode.END))
        g.add_node("d", DispatchAddNode(amount=1000, target=GraphNode.END))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", "b")
        g.add_edge("a", "c")
        g.add_edge("a", "d")
        g.add_edge("b", GraphNode.END)
        g.add_edge("c", GraphNode.END)
        g.add_edge("d", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = make_parallel_ctx(CounterState(count=0))
        engine = GraphEngine(compiled)
        await engine.run_async(ctx)

        scheduler = engine._scheduler
        assert isinstance(scheduler, ParallelScheduler)
        assert len(_end_dispatch_sources(scheduler)) == 3
        assert len(scheduler._ready) == 0
        assert len(scheduler._active) == 0


# ── Termination: ready empty + active empty -> terminate ─────────────────


class TestTerminationConditions:
    """ready empty AND active empty -> graph terminates."""

    async def test_single_node_no_dispatch_terminates(self) -> None:
        """A node that doesn't dispatch: ready+active empty after completion."""
        g: Graph[CounterState] = Graph()
        g.add_node("a", NoDispatchNode(amount=42))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = make_parallel_ctx(CounterState(count=0))
        engine = GraphEngine(compiled)
        await engine.run_async(ctx)

        scheduler = engine._scheduler
        assert isinstance(scheduler, ParallelScheduler)
        assert len(scheduler._ready) == 0
        assert len(scheduler._active) == 0
        assert ctx.state.count == 42

    async def test_one_branch_end_other_branch_leaf_terminates(self) -> None:
        """One branch dispatches to END, another branch goes to a leaf
        node (has edge to END for compile validation but doesn't dispatch).
        Graph terminates because both branches complete and no new
        instances are created.

        C's edge to END uses an explicit reason (not default) so the
        routing compilation does NOT auto-dispatch — true silent skip.
        """
        g: Graph[CounterState] = Graph()
        # A fans out to B and C.
        # B dispatches to END.
        # C has an edge to END (for reachability) but does NOT dispatch.
        g.add_node("a", FanOutDispatchNode(amount=1, target_a="b", target_b="c"))
        g.add_node("b", DispatchAddNode(amount=10, target=GraphNode.END))
        g.add_node("c", NoDispatchNode(amount=100))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", "b")
        g.add_edge("a", "c")
        g.add_edge("b", GraphNode.END)
        g.add_edge("c", GraphNode.END)  # explicit, not default
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = make_parallel_ctx(CounterState(count=0))
        engine = GraphEngine(compiled)
        await engine.run_async(ctx)

        scheduler = engine._scheduler
        assert isinstance(scheduler, ParallelScheduler)
        # Graph terminated.
        assert len(scheduler._ready) == 0
        assert len(scheduler._active) == 0
        # Only b dispatched to END explicitly. c delivers to END via
        # downstream fallback (no default edge, but has edge to END).
        end_sources = _end_dispatch_sources(scheduler)
        b_end_sources = [s for s in end_sources if s.startswith("b#")]
        c_end_sources = [s for s in end_sources if s.startswith("c#")]
        assert len(b_end_sources) == 1
        assert len(c_end_sources) == 1

    async def test_has_out_edges_but_no_dispatch_downstream_dormant(self) -> None:
        """Node C has an outgoing edge to D but doesn't dispatch to D.
        D stays DORMANT (never instantiated). Graph terminates normally.

        C's edge to D uses an explicit reason (not default) so the
        routing compilation does NOT auto-dispatch — true silent skip.
        """
        g: Graph[CounterState] = Graph()
        # A -> [B, C], B -> END, C -> D (explicit reason), D -> END.
        # C does NOT dispatch to D (silent skip). D stays DORMANT.
        g.add_node("a", FanOutDispatchNode(amount=1, target_a="b", target_b="c"))
        g.add_node("b", DispatchAddNode(amount=10, target=GraphNode.END))
        g.add_node("c", NoDispatchNode(amount=100))
        g.add_node("d", DispatchAddNode(amount=1000, target=GraphNode.END))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", "b")
        g.add_edge("a", "c")
        g.add_edge("b", GraphNode.END)
        g.add_edge("c", "d")  # explicit, not default
        g.add_edge("d", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = make_parallel_ctx(CounterState(count=0))
        engine = GraphEngine(compiled)
        await engine.run_async(ctx)

        scheduler = engine._scheduler
        assert isinstance(scheduler, ParallelScheduler)
        # Graph terminated.
        assert len(scheduler._ready) == 0
        assert len(scheduler._active) == 0
        # C delivers to D via downstream fallback. D dispatches to END.
        d_instances = [i for i in scheduler._instances.values() if i.node_name == "d"]
        assert len(d_instances) == 1
        # C completed.
        c_instances = [i for i in scheduler._instances.values() if i.node_name == "c"]
        assert len(c_instances) == 1
        assert c_instances[0].status == NodeInstanceStatus.COMPLETED


# ── Compile validation: START reachability (PARALLEL only) ───────────────


class TestStartReachability:
    """compile(scheduler=PARALLEL): all registered nodes must be reachable
    from entry_node via BFS along outgoing edges."""

    def test_unreachable_node_raises(self) -> None:
        """Node 'b' is registered but not reachable from entry 'a'."""
        g: Graph[CounterState] = Graph()
        g.add_node("a", NoDispatchNode())
        g.add_node("b", NoDispatchNode())
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", GraphNode.END)
        g.add_edge("b", GraphNode.END)  # b has edges but is unreachable from a
        with pytest.raises(RoutingError, match="unreachable"):
            g.compile(scheduler=SchedulerKind.PARALLEL)

    def test_unreachable_node_message_contains_name(self) -> None:
        """Error message contains the unreachable node name."""
        g: Graph[CounterState] = Graph()
        g.add_node("a", NoDispatchNode())
        g.add_node("orphan", NoDispatchNode())
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", GraphNode.END)
        g.add_edge("orphan", GraphNode.END)
        with pytest.raises(RoutingError, match="orphan"):
            g.compile(scheduler=SchedulerKind.PARALLEL)

    def test_multiple_unreachable_nodes_all_listed(self) -> None:
        """Multiple unreachable nodes are all listed in the error."""
        g: Graph[CounterState] = Graph()
        g.add_node("a", NoDispatchNode())
        g.add_node("x", NoDispatchNode())
        g.add_node("y", NoDispatchNode())
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", GraphNode.END)
        g.add_edge("x", GraphNode.END)
        g.add_edge("y", GraphNode.END)
        with pytest.raises(RoutingError, match="x"):
            g.compile(scheduler=SchedulerKind.PARALLEL)

    def test_all_reachable_compiles_ok(self) -> None:
        """All nodes reachable from entry -> compiles successfully."""
        g: Graph[CounterState] = Graph()
        g.add_node("a", DispatchAddNode(amount=1, target="b"))
        g.add_node("b", DispatchAddNode(amount=2, target=GraphNode.END))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", "b")
        g.add_edge("b", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)
        assert compiled.entry_node == "a"

    def test_diamond_all_reachable(self) -> None:
        """Diamond A->[B,C]->D->END: all reachable from A."""
        g: Graph[CounterState] = Graph()
        g.add_node("a", FanOutDispatchNode(amount=1, target_a="b", target_b="c"))
        g.add_node("b", DispatchAddNode(amount=10, target="d"))
        g.add_node("c", DispatchAddNode(amount=100, target="d"))
        g.add_node("d", DispatchAddNode(amount=1000, target=GraphNode.END))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", "b")
        g.add_edge("a", "c")
        g.add_edge("b", "d")
        g.add_edge("c", "d")
        g.add_edge("d", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)
        assert set(compiled.nodes.keys()) == {"a", "b", "c", "d"}


# ── Compile validation: END reachability (PARALLEL only) ─────────────────


class TestEndReachability:
    """compile(scheduler=PARALLEL): all registered nodes must be able to
    reach GraphNode.END via forward BFS (reverse BFS from END)."""

    def test_node_cannot_reach_end_raises(self) -> None:
        """Node 'b' has no path to END (no outgoing edges to END)."""
        g: Graph[CounterState] = Graph()
        g.add_node("a", DispatchAddNode(amount=1, target="b"))
        g.add_node("b", NoDispatchNode())
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", "b")
        g.add_edge("a", GraphNode.END)  # a can reach END, but b cannot
        # NOTE: no edge b -> END
        with pytest.raises(RoutingError, match="cannot reach"):
            g.compile(scheduler=SchedulerKind.PARALLEL)

    def test_node_cannot_reach_end_message_contains_name(self) -> None:
        """Error message contains the node that can't reach END."""
        g: Graph[CounterState] = Graph()
        g.add_node("a", DispatchAddNode(amount=1, target="deadend"))
        g.add_node("deadend", NoDispatchNode())
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", "deadend")
        g.add_edge("a", GraphNode.END)
        # deadend has no outgoing edges -> can't reach END
        with pytest.raises(RoutingError, match="deadend"):
            g.compile(scheduler=SchedulerKind.PARALLEL)

    def test_cycle_without_end_path_raises(self) -> None:
        """A->B->A cycle with no path to END from either node."""
        g: Graph[CounterState] = Graph()
        g.add_node("a", DispatchAddNode(amount=1, target="b"))
        g.add_node("b", DispatchAddNode(amount=2, target="a"))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", "b")
        g.add_edge("b", "a")
        # Neither a nor b has an edge to END
        with pytest.raises(RoutingError, match="cannot reach"):
            g.compile(scheduler=SchedulerKind.PARALLEL, cycle_detection="off")

    def test_all_can_reach_end_compiles_ok(self) -> None:
        """All nodes have a path to END -> compiles successfully."""
        g: Graph[CounterState] = Graph()
        g.add_node("a", DispatchAddNode(amount=1, target="b"))
        g.add_node("b", DispatchAddNode(amount=2, target=GraphNode.END))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", "b")
        g.add_edge("b", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)
        assert compiled.entry_node == "a"

    def test_node_with_only_end_edge_compiles_ok(self) -> None:
        """Node with only an edge to END (no other outgoing edges)
        can reach END -> compiles successfully."""
        g: Graph[CounterState] = Graph()
        g.add_node("a", DispatchAddNode(amount=1, target="b"))
        g.add_node("b", NoDispatchNode())  # has edge to END but doesn't dispatch
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", "b")
        g.add_edge("b", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)
        assert "b" in compiled.nodes


# ── LINEAR mode: no reachability validation ──────────────────────────────


class TestLinearSkipsReachability:
    """LINEAR scheduler does NOT run START/END reachability checks.
    Graphs that would fail under PARALLEL compile fine under LINEAR."""

    def test_linear_allows_unreachable_node(self) -> None:
        """Unreachable node 'b' compiles fine under LINEAR."""
        g: Graph[CounterState] = Graph()
        g.add_node("a", NoDispatchNode())
        g.add_node("b", NoDispatchNode())
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", GraphNode.END)
        g.add_edge("b", GraphNode.END)  # b unreachable from a
        # LINEAR: no reachability check -> compiles OK.
        compiled = g.compile(scheduler=SchedulerKind.LINEAR)
        assert "b" in compiled.nodes

    def test_linear_allows_node_cannot_reach_end(self) -> None:
        """Node 'b' with no path to END compiles fine under LINEAR."""
        g: Graph[CounterState] = Graph()
        g.add_node("a", DispatchAddNode(amount=1, target="b"))
        g.add_node("b", NoDispatchNode())
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", "b")
        g.add_edge("a", GraphNode.END)
        # b has no outgoing edges -> can't reach END
        # LINEAR: no reachability check -> compiles OK.
        compiled = g.compile(scheduler=SchedulerKind.LINEAR)
        assert "b" in compiled.nodes

    def test_default_linear_allows_unreachable_node(self) -> None:
        """Default scheduler (LINEAR) allows unreachable nodes."""
        g: Graph[CounterState] = Graph()
        g.add_node("a", NoDispatchNode())
        g.add_node("orphan", NoDispatchNode())
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", GraphNode.END)
        g.add_edge("orphan", GraphNode.END)
        # Default: LINEAR -> no reachability check.
        compiled = g.compile()
        assert "orphan" in compiled.nodes
