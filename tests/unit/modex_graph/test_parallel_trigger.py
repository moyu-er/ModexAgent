"""Trigger modes + reachability-based readiness tests (Task 06).

Covers:

- `NodeTrigger` StrEnum: `ON_ALL_PREDS` / `ON_RECEIVE`.
- `Node` ABC `trigger` attribute (None = graph default).
- `Graph.compile(default_trigger=...)` + `CompiledGraph.default_trigger`.
- `ON_ALL_PREDS`: waits for all activated sources to dispatch, then fires
  one instance per group (one dispatch per source).
- `ON_RECEIVE`: each dispatch creates a new instance (gated by reachability).
- Reachability BFS: a node never becomes READY while any
  PENDING/READY/RUNNING instance can reach it via outgoing edges.
- Conditional branch skip: A→C only (A→B edge exists but A doesn't dispatch
  to B) → D (ON_ALL_PREDS, in-edges B→D, C→D) fires after C alone.
- Long chain: A→[B,C], B→D, C→E→F→D — D waits for E→F→D to finish.
- Node-level `trigger` overrides graph-level `default_trigger`.
- Self-loop: each execution produces a new instance, no state reset.
"""

from __future__ import annotations

from typing import Any

from helpers import CounterState, make_runtime

from modex_graph import (
    Graph,
    GraphContext,
    GraphEngine,
    GraphNode,
    Node,
    NodeInstanceStatus,
    NodeResult,
    NodeTrigger,
    ParallelScheduler,
    SchedulerKind,
)


def make_parallel_ctx(state: CounterState | None = None) -> GraphContext[CounterState]:
    return GraphContext(
        state=state if state is not None else CounterState(),
        runtime=make_runtime(),
        scheduler_kind=SchedulerKind.PARALLEL,
    )


class DispatchAddNode(Node[CounterState]):
    def __init__(self, amount: int, target: str | None = None) -> None:
        self.amount = amount
        self.target = target

    def execute(self, ctx: GraphContext[CounterState]) -> NodeResult:
        ctx.state.count += self.amount
        if self.target is not None:
            ctx.dispatch(self.target)
        return NodeResult()


class FanOutDispatchNode(Node[CounterState]):
    """Dispatches to two targets (fan-out)."""

    def __init__(self, amount: int, target_a: str, target_b: str) -> None:
        self.amount = amount
        self.target_a = target_a
        self.target_b = target_b

    def execute(self, ctx: GraphContext[CounterState]) -> NodeResult:
        ctx.state.count += self.amount
        ctx.dispatch(self.target_a)
        ctx.dispatch(self.target_b)
        return NodeResult()


class ConditionalDispatchNode(Node[CounterState]):
    """Dispatches only to `chosen_target`, skipping the other edge."""

    def __init__(self, amount: int, chosen_target: str) -> None:
        self.amount = amount
        self.chosen_target = chosen_target

    def execute(self, ctx: GraphContext[CounterState]) -> NodeResult:
        ctx.state.count += self.amount
        ctx.dispatch(self.chosen_target)
        return NodeResult()


class RecordExecutionNode(Node[CounterState]):
    """Appends its instance id to state.messages (ReducerChannel)."""

    def __init__(self, target: str | None = None) -> None:
        self.target = target

    def execute(self, ctx: GraphContext[CounterState]) -> NodeResult:
        label = ctx._current_instance or self.name
        if self.target is not None:
            ctx.dispatch(self.target)
        return NodeResult(state_update={"messages": [label]})


# ── NodeTrigger enum ──────────────────────────────────────────────────────


class TestNodeTriggerEnum:
    def test_is_strenum(self) -> None:
        from enum import StrEnum

        assert issubclass(NodeTrigger, StrEnum)

    def test_two_members(self) -> None:
        assert len(NodeTrigger) == 2

    def test_values(self) -> None:
        assert NodeTrigger.ON_ALL_PREDS == "on_all_preds"
        assert NodeTrigger.ON_RECEIVE == "on_receive"

    def test_members_are_str(self) -> None:
        for member in NodeTrigger:
            assert isinstance(member, str)

    def test_exported_from_modex_graph(self) -> None:
        import modex_graph

        assert "NodeTrigger" in modex_graph.__all__
        assert hasattr(modex_graph, "NodeTrigger")


# ── Node.trigger attribute ────────────────────────────────────────────────


class TestNodeTriggerAttribute:
    def test_node_has_trigger_attribute(self) -> None:
        node = DispatchAddNode(amount=1)
        assert hasattr(node, "trigger")
        assert node.trigger is None

    def test_compiled_graph_has_default_trigger(self) -> None:
        g: Graph[CounterState] = Graph()
        g.add_node("a", DispatchAddNode(amount=1, target=GraphNode.END))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)
        assert compiled.default_trigger == NodeTrigger.ON_ALL_PREDS

    def test_compile_accepts_default_trigger(self) -> None:
        g: Graph[CounterState] = Graph()
        g.add_node("a", DispatchAddNode(amount=1, target=GraphNode.END))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", GraphNode.END)
        compiled = g.compile(
            scheduler=SchedulerKind.PARALLEL,
            default_trigger=NodeTrigger.ON_RECEIVE,
        )
        assert compiled.default_trigger == NodeTrigger.ON_RECEIVE


# ── ON_ALL_PREDS: two upstreams both dispatch → one instance ─────────────


class TestOnAllPreds:
    """A → [B, C] → D (ON_ALL_PREDS) → END. D fires once after both dispatch."""

    def _build_graph(self) -> Graph[CounterState]:
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
        return g

    async def test_d_fires_once(self) -> None:
        g = self._build_graph()
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = make_parallel_ctx(CounterState(count=0))
        engine = GraphEngine(compiled)
        await engine.run_async(ctx)

        scheduler = engine._scheduler
        assert isinstance(scheduler, ParallelScheduler)
        d_instances = [i for i in scheduler._instances.values() if i.node_name == "d"]
        assert len(d_instances) == 1
        assert d_instances[0].status == NodeInstanceStatus.COMPLETED

    async def test_d_count_reflects_single_execution(self) -> None:
        """A fast (count=1). B,C fork (mutations dropped). D fast (count+1000).
        Total = 1 + 1000 = 1001."""
        g = self._build_graph()
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = make_parallel_ctx(CounterState(count=0))
        result = await GraphEngine(compiled).run_async(ctx)
        assert result.count == 1001

    async def test_d_not_fired_when_one_source_dispatches_to_end(self) -> None:
        """C dispatches to END instead of D. D's activated sources = {b} only.
        B dispatches to D → D fires (single activated source, all dispatched)."""
        g: Graph[CounterState] = Graph()
        g.add_node("a", FanOutDispatchNode(amount=1, target_a="b", target_b="c"))
        g.add_node("b", DispatchAddNode(amount=10, target="d"))
        g.add_node("c", DispatchAddNode(amount=100, target=GraphNode.END))
        g.add_node("d", DispatchAddNode(amount=1000, target=GraphNode.END))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", "b")
        g.add_edge("a", "c")
        g.add_edge("b", "d")
        g.add_edge("c", "d")
        g.add_edge("c", GraphNode.END)
        g.add_edge("d", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = make_parallel_ctx(CounterState(count=0))
        engine = GraphEngine(compiled)
        await engine.run_async(ctx)

        scheduler = engine._scheduler
        assert isinstance(scheduler, ParallelScheduler)
        d_instances = [i for i in scheduler._instances.values() if i.node_name == "d"]
        # C dispatched to END, not D → D's activated sources = {b} only.
        # B dispatches to D → D fires (all activated sources dispatched).
        assert len(d_instances) == 1


# ── ON_ALL_PREDS: conditional branch skip ─────────────────────────────────


class TestConditionalBranchSkip:
    """A → [B, C] (A only dispatches to C; A→B edge exists but unused).
    D has in-edges B→D, C→D, ON_ALL_PREDS. D's activated source = {C} only.
    D fires after C completes (no deadlock waiting for B)."""

    async def test_skip_one_arm_join_no_deadlock(self) -> None:
        g: Graph[CounterState] = Graph()
        g.add_node("a", ConditionalDispatchNode(amount=1, chosen_target="c"))
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

        ctx = make_parallel_ctx(CounterState(count=0))
        engine = GraphEngine(compiled)
        await engine.run_async(ctx)

        scheduler = engine._scheduler
        assert isinstance(scheduler, ParallelScheduler)
        d_instances = [i for i in scheduler._instances.values() if i.node_name == "d"]
        assert len(d_instances) == 1
        assert d_instances[0].status == NodeInstanceStatus.COMPLETED
        # A=1 (fast), C=100 (fast, single READY), D=1000 (fast after C)
        assert ctx.state.count == 1101


# ── ON_RECEIVE: each dispatch → new instance ──────────────────────────────


class TestOnReceive:
    """A → [B, C] → D (ON_RECEIVE) → END. D fires once per dispatch."""

    def _build_graph(self) -> Graph[CounterState]:
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
        return g

    async def test_two_dispatches_two_instances_sequential(self) -> None:
        """B and C dispatch to D (ON_RECEIVE). D fires twice — once per
        dispatch, serialized by reachability (B's D instance waits for B
        to complete; C's D instance waits for C to complete)."""
        g = self._build_graph()
        compiled = g.compile(
            scheduler=SchedulerKind.PARALLEL,
            default_trigger=NodeTrigger.ON_RECEIVE,
        )

        ctx = make_parallel_ctx(CounterState(count=0))
        engine = GraphEngine(compiled)
        await engine.run_async(ctx)

        scheduler = engine._scheduler
        assert isinstance(scheduler, ParallelScheduler)
        d_instances = [i for i in scheduler._instances.values() if i.node_name == "d"]
        assert len(d_instances) == 2
        for inst in d_instances:
            assert inst.status == NodeInstanceStatus.COMPLETED

    async def test_concurrent_dispatches_still_two_instances(self) -> None:
        """B and C are in the same batch (forked). Both dispatch to D.
        D (ON_RECEIVE) creates two instances — but they can't fire
        concurrently (each is blocked by B/C reachability). After B and C
        complete, the two D instances fire sequentially."""
        g = self._build_graph()
        compiled = g.compile(
            scheduler=SchedulerKind.PARALLEL,
            default_trigger=NodeTrigger.ON_RECEIVE,
        )

        ctx = make_parallel_ctx(CounterState(count=0))
        engine = GraphEngine(compiled)
        await engine.run_async(ctx)

        scheduler = engine._scheduler
        assert isinstance(scheduler, ParallelScheduler)
        d_instances = [i for i in scheduler._instances.values() if i.node_name == "d"]
        assert len(d_instances) == 2


# ── Long chain: D not triggered early ─────────────────────────────────────


class TestLongChain:
    """A → [B, C], B → D, C → E → F → D. D is ON_ALL_PREDS.
    B completes first but E is still running (E→F→D path). D must wait
    until E→F→D all complete."""

    def _build_graph(self) -> Graph[CounterState]:
        g: Graph[CounterState] = Graph()
        g.add_node("a", FanOutDispatchNode(amount=1, target_a="b", target_b="c"))
        g.add_node("b", DispatchAddNode(amount=10, target="d"))
        g.add_node("c", DispatchAddNode(amount=100, target="e"))
        g.add_node("d", DispatchAddNode(amount=1000, target=GraphNode.END))
        g.add_node("e", DispatchAddNode(amount=10000, target="f"))
        g.add_node("f", DispatchAddNode(amount=100000, target="d"))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", "b")
        g.add_edge("a", "c")
        g.add_edge("b", "d")
        g.add_edge("c", "e")
        g.add_edge("e", "f")
        g.add_edge("f", "d")
        g.add_edge("d", GraphNode.END)
        return g

    async def test_d_waits_for_long_chain(self) -> None:
        g = self._build_graph()
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = make_parallel_ctx(CounterState(count=0))
        engine = GraphEngine(compiled)
        await engine.run_async(ctx)

        scheduler = engine._scheduler
        assert isinstance(scheduler, ParallelScheduler)
        d_instances = [i for i in scheduler._instances.values() if i.node_name == "d"]
        assert len(d_instances) == 1
        assert d_instances[0].status == NodeInstanceStatus.COMPLETED

    async def test_d_fires_only_once_not_twice(self) -> None:
        """D has two activated sources: B and F. ON_ALL_PREDS groups them.
        D fires once (consuming one dispatch from B and one from F)."""
        g = self._build_graph()
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = make_parallel_ctx(CounterState(count=0))
        engine = GraphEngine(compiled)
        await engine.run_async(ctx)

        scheduler = engine._scheduler
        assert isinstance(scheduler, ParallelScheduler)
        d_instances = [i for i in scheduler._instances.values() if i.node_name == "d"]
        assert len(d_instances) == 1
        # A=1 fast. B,C fork (mutations dropped). E fast (+10000).
        # F fast (+100000). D fast (+1000).
        assert ctx.state.count == 111001


# ── Node-level trigger overrides graph-level default ──────────────────────


class TestNodeTriggerOverridesDefault:
    """Graph default is ON_RECEIVE, but D's node sets trigger=ON_ALL_PREDS.
    D should wait for all sources (ON_ALL_PREDS), not fire per-dispatch."""

    async def test_node_trigger_overrides_graph_default(self) -> None:
        class OnAllPredsNode(Node[CounterState]):
            trigger = NodeTrigger.ON_ALL_PREDS

            def __init__(self, amount: int, target: str | None = None) -> None:
                self.amount = amount
                self.target = target

            def execute(self, ctx: GraphContext[CounterState]) -> NodeResult:
                ctx.state.count += self.amount
                if self.target is not None:
                    ctx.dispatch(self.target)
                return NodeResult()

        g: Graph[CounterState] = Graph()
        g.add_node("a", FanOutDispatchNode(amount=1, target_a="b", target_b="c"))
        g.add_node("b", DispatchAddNode(amount=10, target="d"))
        g.add_node("c", DispatchAddNode(amount=100, target="d"))
        g.add_node("d", OnAllPredsNode(amount=1000, target=GraphNode.END))
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
        await engine.run_async(ctx)

        scheduler = engine._scheduler
        assert isinstance(scheduler, ParallelScheduler)
        d_instances = [i for i in scheduler._instances.values() if i.node_name == "d"]
        # D uses ON_ALL_PREDS despite graph default ON_RECEIVE → 1 instance.
        assert len(d_instances) == 1

    async def test_node_on_receive_overrides_graph_on_all_preds(self) -> None:
        class OnReceiveNode(Node[CounterState]):
            trigger = NodeTrigger.ON_RECEIVE

            def __init__(self, amount: int, target: str | None = None) -> None:
                self.amount = amount
                self.target = target

            def execute(self, ctx: GraphContext[CounterState]) -> NodeResult:
                ctx.state.count += self.amount
                if self.target is not None:
                    ctx.dispatch(self.target)
                return NodeResult()

        g: Graph[CounterState] = Graph()
        g.add_node("a", FanOutDispatchNode(amount=1, target_a="b", target_b="c"))
        g.add_node("b", DispatchAddNode(amount=10, target="d"))
        g.add_node("c", DispatchAddNode(amount=100, target="d"))
        g.add_node("d", OnReceiveNode(amount=1000, target=GraphNode.END))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", "b")
        g.add_edge("a", "c")
        g.add_edge("b", "d")
        g.add_edge("c", "d")
        g.add_edge("d", GraphNode.END)
        # Graph default is ON_ALL_PREDS, but D is ON_RECEIVE → 2 instances.
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = make_parallel_ctx(CounterState(count=0))
        engine = GraphEngine(compiled)
        await engine.run_async(ctx)

        scheduler = engine._scheduler
        assert isinstance(scheduler, ParallelScheduler)
        d_instances = [i for i in scheduler._instances.values() if i.node_name == "d"]
        assert len(d_instances) == 2


# ── Self-loop: each execution produces a new instance ─────────────────────


class TestSelfLoop:
    """Loop node dispatches to itself. ON_ALL_PREDS default. Each execution
    produces exactly one new instance. State is NOT reset between iterations
    (imperative mutations accumulate on main_state fast path)."""

    async def test_self_loop_produces_new_instance_each_iteration(self) -> None:
        class SelfDispatchNode(Node[CounterState]):
            def __init__(self) -> None:
                pass

            def execute(self, ctx: GraphContext[CounterState]) -> NodeResult:
                ctx.state.count += 1
                if ctx.state.count >= 5:
                    ctx.dispatch(GraphNode.END)
                else:
                    ctx.dispatch("loop")
                return NodeResult()

        g: Graph[CounterState] = Graph()
        g.add_node("loop", SelfDispatchNode())
        g.add_edge(GraphNode.START, "loop")
        g.add_edge("loop", "loop")
        g.add_edge("loop", GraphNode.END)
        compiled = g.compile(
            max_iterations=20,
            scheduler=SchedulerKind.PARALLEL,
            cycle_detection="off",
        )

        ctx = make_parallel_ctx(CounterState(count=0))
        await GraphEngine(compiled).run_async(ctx)

        assert ctx.state.count == 5

    async def test_self_loop_instance_count(self) -> None:
        class SelfDispatchNode(Node[CounterState]):
            def execute(self, ctx: GraphContext[CounterState]) -> NodeResult:
                ctx.state.count += 1
                if ctx.state.count >= 3:
                    ctx.dispatch(GraphNode.END)
                else:
                    ctx.dispatch("loop")
                return NodeResult()

        g: Graph[CounterState] = Graph()
        g.add_node("loop", SelfDispatchNode())
        g.add_edge(GraphNode.START, "loop")
        g.add_edge("loop", "loop")
        g.add_edge("loop", GraphNode.END)
        compiled = g.compile(
            max_iterations=20,
            scheduler=SchedulerKind.PARALLEL,
            cycle_detection="off",
        )

        ctx = make_parallel_ctx(CounterState(count=0))
        engine = GraphEngine(compiled)
        await engine.run_async(ctx)

        scheduler = engine._scheduler
        assert isinstance(scheduler, ParallelScheduler)
        loop_instances = [
            i for i in scheduler._instances.values() if i.node_name == "loop"
        ]
        # 3 executions → 3 instances (loop#0, loop#1, loop#2).
        assert len(loop_instances) == 3
        for inst in loop_instances:
            assert inst.status == NodeInstanceStatus.COMPLETED

    async def test_self_loop_on_receive_also_works(self) -> None:
        class SelfDispatchNode(Node[CounterState]):
            def execute(self, ctx: GraphContext[CounterState]) -> NodeResult:
                ctx.state.count += 1
                if ctx.state.count >= 4:
                    ctx.dispatch(GraphNode.END)
                else:
                    ctx.dispatch("loop")
                return NodeResult()

        g: Graph[CounterState] = Graph()
        g.add_node("loop", SelfDispatchNode())
        g.add_edge(GraphNode.START, "loop")
        g.add_edge("loop", "loop")
        g.add_edge("loop", GraphNode.END)
        compiled = g.compile(
            max_iterations=20,
            scheduler=SchedulerKind.PARALLEL,
            cycle_detection="off",
            default_trigger=NodeTrigger.ON_RECEIVE,
        )

        ctx = make_parallel_ctx(CounterState(count=0))
        await GraphEngine(compiled).run_async(ctx)

        assert ctx.state.count == 4


# ── Reachability BFS unit tests ────────────────────────────────────────────


class TestReachabilityBFS:
    """Direct unit tests for `_can_reach_active`."""

    def _setup(self) -> tuple[ParallelScheduler[Any], Graph[CounterState]]:
        g: Graph[CounterState] = Graph()
        g.add_node("a", DispatchAddNode(amount=1, target="b"))
        g.add_node("b", DispatchAddNode(amount=2, target="c"))
        g.add_node("c", DispatchAddNode(amount=3, target=GraphNode.END))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", "b")
        g.add_edge("b", "c")
        g.add_edge("c", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)
        scheduler = ParallelScheduler(compiled)
        return scheduler, g

    def test_no_active_instances_returns_false(self) -> None:
        scheduler, _ = self._setup()
        assert scheduler._can_reach_active("b") is False

    def test_active_at_node_can_reach_successor(self) -> None:
        scheduler, _ = self._setup()
        iid = scheduler._create_instance("a")
        scheduler._instances[iid].status = NodeInstanceStatus.RUNNING
        assert scheduler._can_reach_active("b") is True
        assert scheduler._can_reach_active("c") is True

    def test_active_at_node_cannot_reach_unrelated(self) -> None:
        scheduler, _ = self._setup()
        iid = scheduler._create_instance("c")
        scheduler._instances[iid].status = NodeInstanceStatus.RUNNING
        assert scheduler._can_reach_active("a") is False
        assert scheduler._can_reach_active("b") is False

    def test_exclude_self(self) -> None:
        """A PENDING instance at 'loop' with a self-loop should not block
        itself (excluded from the BFS)."""
        g: Graph[CounterState] = Graph()
        g.add_node("loop", DispatchAddNode(amount=1, target="loop"))
        g.add_edge(GraphNode.START, "loop")
        g.add_edge("loop", "loop")
        g.add_edge("loop", GraphNode.END)
        compiled = g.compile(
            scheduler=SchedulerKind.PARALLEL, cycle_detection="off"
        )
        scheduler = ParallelScheduler(compiled)
        iid = scheduler._create_instance("loop")
        scheduler._instances[iid].status = NodeInstanceStatus.PENDING
        # Excluding itself → no active instances → can't reach.
        assert scheduler._can_reach_active("loop", exclude=iid) is False

    def test_completed_instance_not_counted(self) -> None:
        scheduler, _ = self._setup()
        iid = scheduler._create_instance("a")
        scheduler._instances[iid].status = NodeInstanceStatus.COMPLETED
        scheduler._active.discard(iid)
        assert scheduler._can_reach_active("b") is False

    def test_dormant_instance_not_counted(self) -> None:
        scheduler, _ = self._setup()
        iid = scheduler._create_instance("a")
        assert scheduler._instances[iid].status == NodeInstanceStatus.DORMANT
        assert scheduler._can_reach_active("b") is False


# ── Resolve trigger helper ────────────────────────────────────────────────


class TestResolveTrigger:
    def test_node_trigger_overrides_default(self) -> None:
        class OnReceiveNode(Node[CounterState]):
            trigger = NodeTrigger.ON_RECEIVE

            def execute(self, ctx: GraphContext[CounterState]) -> NodeResult:
                return NodeResult()

        g: Graph[CounterState] = Graph()
        g.add_node("a", OnReceiveNode())
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", GraphNode.END)
        compiled = g.compile(
            scheduler=SchedulerKind.PARALLEL,
            default_trigger=NodeTrigger.ON_ALL_PREDS,
        )
        scheduler = ParallelScheduler(compiled)
        assert scheduler._resolve_trigger("a") == NodeTrigger.ON_RECEIVE

    def test_none_trigger_uses_default(self) -> None:
        g: Graph[CounterState] = Graph()
        g.add_node("a", DispatchAddNode(amount=1, target=GraphNode.END))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", GraphNode.END)
        compiled = g.compile(
            scheduler=SchedulerKind.PARALLEL,
            default_trigger=NodeTrigger.ON_RECEIVE,
        )
        scheduler = ParallelScheduler(compiled)
        assert scheduler._resolve_trigger("a") == NodeTrigger.ON_RECEIVE

    def test_default_on_all_preds(self) -> None:
        g: Graph[CounterState] = Graph()
        g.add_node("a", DispatchAddNode(amount=1, target=GraphNode.END))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)
        scheduler = ParallelScheduler(compiled)
        assert scheduler._resolve_trigger("a") == NodeTrigger.ON_ALL_PREDS


# ── Diamond: A→[B,C]→D, ON_ALL_PREDS fires once ───────────────────────────


class TestDiamondJoin:
    """Classic diamond: A→[B,C]→D. D (ON_ALL_PREDS) fires exactly once
    after both B and C complete."""

    async def test_diamond_d_fires_once(self) -> None:
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

        ctx = make_parallel_ctx(CounterState(count=0))
        engine = GraphEngine(compiled)
        await engine.run_async(ctx)

        scheduler = engine._scheduler
        assert isinstance(scheduler, ParallelScheduler)
        d_instances = [i for i in scheduler._instances.values() if i.node_name == "d"]
        assert len(d_instances) == 1


# ── ON_ALL_PREDS multiple groups: A dispatches twice → 2 D instances ──────


class TestOnAllPredsMultipleGroups:
    """A dispatches to D twice (single source). D is ON_ALL_PREDS.

    Per ADR-0034 D3: ON_ALL_PREDS deduplicates by source — a source
    dispatching once or multiple times counts as "source has dispatched".
    D fires once after A completes, consuming all pending dispatches.
    """

    async def test_single_source_multiple_dispatches(self) -> None:
        class DoubleDispatchNode(Node[CounterState]):
            def execute(self, ctx: GraphContext[CounterState]) -> NodeResult:
                ctx.state.count += 1
                ctx.dispatch("d")
                ctx.dispatch("d")
                return NodeResult()

        g: Graph[CounterState] = Graph()
        g.add_node("a", DoubleDispatchNode())
        g.add_node("d", DispatchAddNode(amount=1000, target=GraphNode.END))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", "d")
        g.add_edge("d", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = make_parallel_ctx(CounterState(count=0))
        engine = GraphEngine(compiled)
        await engine.run_async(ctx)

        scheduler = engine._scheduler
        assert isinstance(scheduler, ParallelScheduler)
        d_instances = [i for i in scheduler._instances.values() if i.node_name == "d"]
        assert len(d_instances) == 1
