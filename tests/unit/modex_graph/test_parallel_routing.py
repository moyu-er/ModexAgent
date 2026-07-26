"""Routing compilation tests for ParallelScheduler (Task 04).

Covers the Task 04 acceptance criteria:

- `transition="success"` matching two edges → B and C both dispatched (fan-out).
- `Command(goto=[Task(node="B"), Task(node="C")])` → B and C parallel dispatch.
- Node manually `ctx.dispatch("D")` + returns `transition="done"` matching E →
  D and E both dispatched (mixed mode, not mutually exclusive).
- Node does not dispatch and does not return transition → silent skip.
- `transition="nonexistent"` with no matching edge and no default edge →
  `RoutingError`.
- Fan-out + fan-in end-to-end (A → [B, C] → D). D's trigger mode uses the
  simple ON_RECEIVE stand-in (ready on first dispatch); full ON_ALL_PREDS
  arrives in Task 06.
- `NodeResult.state_update` is carried as the dispatch payload for compiled
  dispatches.
- `CompiledGraph.next_nodes_by_transition` / `default_edge_targets` return
  `list[str]` (all matches, not first).
"""
from __future__ import annotations

import pytest
from helpers import CounterState, make_runtime

from modex_graph import (
    Command,
    Graph,
    GraphContext,
    GraphEngine,
    GraphNode,
    Node,
    NodeResult,
    NodeTrigger,
    ParallelScheduler,
    RoutingError,
    SchedulerKind,
    Task,
)


def make_parallel_ctx(state: CounterState | None = None) -> GraphContext[CounterState]:
    return GraphContext(
        state=state if state is not None else CounterState(),
        runtime=make_runtime(),
        scheduler_kind=SchedulerKind.PARALLEL,
    )


class AddTransitionNode(Node[CounterState]):
    """Increments count, returns NodeResult(transition=...)."""

    def __init__(self, amount: int, transition: str) -> None:
        self.amount = amount
        self.transition = transition

    def execute(self, ctx: GraphContext[CounterState]) -> NodeResult:
        ctx.state.count += self.amount
        return NodeResult(transition=self.transition)


class AddCommandNode(Node[CounterState]):
    """Increments count, returns NodeResult(command=Command(goto=...))."""

    def __init__(self, amount: int, goto: str | list[Task] | None) -> None:
        self.amount = amount
        self.goto = goto

    def execute(self, ctx: GraphContext[CounterState]) -> NodeResult:
        ctx.state.count += self.amount
        return NodeResult(command=Command(goto=self.goto))


class AddStateUpdateTransitionNode(Node[CounterState]):
    """Returns transition + state_update (state_update should become payload)."""

    def __init__(self, transition: str, label: str) -> None:
        self.transition = transition
        self.label = label

    def execute(self, ctx: GraphContext[CounterState]) -> NodeResult:
        return NodeResult(
            transition=self.transition,
            state_update={"messages": [self.label]},
        )


class ManualDispatchPlusTransitionNode(Node[CounterState]):
    """Manually dispatches to one target AND returns a transition.

    Tests the mixed mode: both the manual dispatch and the transition-matched
    edge should fire (not mutually exclusive).
    """

    def __init__(self, manual_target: str, transition: str, amount: int = 1) -> None:
        self.manual_target = manual_target
        self.transition = transition
        self.amount = amount

    def execute(self, ctx: GraphContext[CounterState]) -> NodeResult:
        ctx.state.count += self.amount
        ctx.dispatch(self.manual_target)
        return NodeResult(transition=self.transition)


class NoOpNode(Node[CounterState]):
    """Does nothing — no dispatch, no transition. Tests silent skip."""

    def execute(self, ctx: GraphContext[CounterState]) -> NodeResult:
        return NodeResult()


class AddAndDispatchNode(Node[CounterState]):
    """Increments count, dispatches to a target."""

    def __init__(self, amount: int, target: str) -> None:
        self.amount = amount
        self.target = target

    def execute(self, ctx: GraphContext[CounterState]) -> NodeResult:
        ctx.state.count += self.amount
        ctx.dispatch(self.target)
        return NodeResult()


# ── CompiledGraph plural edge-lookup methods ──────────────────────────────


class TestCompiledGraphPluralLookups:
    def test_next_nodes_by_transition_returns_all_matches(self) -> None:
        g: Graph[CounterState] = Graph()
        g.add_node("a", NoOpNode())
        g.add_node("b", NoOpNode())
        g.add_node("c", NoOpNode())
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", "b", reason="success")
        g.add_edge("a", "c", reason="success")
        compiled = g.compile()

        targets = compiled.next_nodes_by_transition("a", "success")
        assert targets == ["b", "c"]

    def test_next_nodes_by_transition_empty_when_no_match(self) -> None:
        g: Graph[CounterState] = Graph()
        g.add_node("a", NoOpNode())
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", GraphNode.END, reason="other")
        compiled = g.compile()

        assert compiled.next_nodes_by_transition("a", "success") == []

    def test_default_edge_targets_returns_all(self) -> None:
        g: Graph[CounterState] = Graph()
        g.add_node("a", NoOpNode())
        g.add_node("b", NoOpNode())
        g.add_node("c", NoOpNode())
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", "b", reason=None)
        g.add_edge("a", "c", reason=None)
        compiled = g.compile()

        targets = compiled.default_edge_targets("a")
        assert targets == ["b", "c"]

    def test_default_edge_targets_empty_when_none(self) -> None:
        g: Graph[CounterState] = Graph()
        g.add_node("a", NoOpNode())
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", GraphNode.END, reason="explicit")
        compiled = g.compile()

        assert compiled.default_edge_targets("a") == []

    def test_singular_methods_still_work_via_delegation(self) -> None:
        """Singular methods delegate to plural and return first match."""
        g: Graph[CounterState] = Graph()
        g.add_node("a", NoOpNode())
        g.add_node("b", NoOpNode())
        g.add_node("c", NoOpNode())
        g.add_node("d", NoOpNode())
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", "b", reason="success")
        g.add_edge("a", "c", reason="success")
        g.add_edge("a", "d", reason=None)
        g.add_edge("b", GraphNode.END)
        g.add_edge("c", GraphNode.END)
        g.add_edge("d", GraphNode.END)
        compiled = g.compile()

        assert compiled.next_node_by_transition("a", "success") == "b"
        assert compiled.default_edge_target("a") == "d"

    def test_singular_returns_none_when_no_match(self) -> None:
        g: Graph[CounterState] = Graph()
        g.add_node("a", NoOpNode())
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", GraphNode.END, reason="other")
        compiled = g.compile()

        assert compiled.next_node_by_transition("a", "success") is None
        assert compiled.default_edge_target("a") is None


# ── transition fan-out: multiple same-reason edges all fire ───────────────


class TestTransitionFanOut:
    async def test_transition_matches_two_edges_both_dispatched(self) -> None:
        """A→B reason='success' + A→C reason='success' → B and C both run."""
        g: Graph[CounterState] = Graph()
        g.add_node("a", AddTransitionNode(amount=1, transition="success"))
        g.add_node("b", AddAndDispatchNode(amount=10, target=GraphNode.END))
        g.add_node("c", AddAndDispatchNode(amount=100, target=GraphNode.END))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", "b", reason="success")
        g.add_edge("a", "c", reason="success")
        g.add_edge("b", GraphNode.END)
        g.add_edge("c", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = make_parallel_ctx(CounterState(count=0))
        result = await GraphEngine(compiled).run_async(ctx)
        # Fork isolation (Task 05): B, C execute on forked states — their
        # imperative count mutations do NOT propagate to main_state. Only
        # A's mutation (fast path) persists. Instance-count assertions in
        # test_transition_fan_out_creates_two_instances verify B, C ran.
        assert result.count == 1

    async def test_transition_fan_out_creates_two_instances(self) -> None:
        g: Graph[CounterState] = Graph()
        g.add_node("a", AddTransitionNode(amount=1, transition="success"))
        g.add_node("b", AddAndDispatchNode(amount=10, target=GraphNode.END))
        g.add_node("c", AddAndDispatchNode(amount=100, target=GraphNode.END))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", "b", reason="success")
        g.add_edge("a", "c", reason="success")
        g.add_edge("b", GraphNode.END)
        g.add_edge("c", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = make_parallel_ctx(CounterState(count=0))
        engine = GraphEngine(compiled)
        await engine.run_async(ctx)

        scheduler = engine._scheduler
        assert isinstance(scheduler, ParallelScheduler)
        b_instances = [i for i in scheduler._instances.values() if i.node_name == "b"]
        c_instances = [i for i in scheduler._instances.values() if i.node_name == "c"]
        assert len(b_instances) == 1
        assert len(c_instances) == 1

    async def test_transition_state_update_carried_as_payload(self) -> None:
        """NodeResult.state_update becomes the dispatch payload."""
        g: Graph[CounterState] = Graph()
        g.add_node(
            "a",
            AddStateUpdateTransitionNode(transition="success", label="from_a"),
        )
        g.add_node("b", AddAndDispatchNode(amount=0, target=GraphNode.END))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", "b", reason="success")
        g.add_edge("b", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = make_parallel_ctx(CounterState())
        engine = GraphEngine(compiled)
        await engine.run_async(ctx)

        scheduler = engine._scheduler
        assert isinstance(scheduler, ParallelScheduler)
        # The compiled dispatch to "b" should carry state_update as payload.
        a_to_b = [
            e for e in scheduler._dispatch_log if e.source_instance == "a#0" and e.target == "b"
        ]
        assert len(a_to_b) == 1
        assert a_to_b[0].payload == {"messages": ["from_a"]}

    async def test_transition_no_match_falls_back_to_default(self) -> None:
        """transition with no matching edge falls through to default edge."""
        g: Graph[CounterState] = Graph()
        g.add_node("a", AddTransitionNode(amount=1, transition="nonexistent"))
        g.add_node("b", AddAndDispatchNode(amount=10, target=GraphNode.END))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", "b", reason=None)
        g.add_edge("b", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = make_parallel_ctx(CounterState(count=0))
        result = await GraphEngine(compiled).run_async(ctx)
        assert result.count == 11


# ── Command.goto compilation ──────────────────────────────────────────────


class TestCommandGotoStr:
    async def test_goto_str_dispatches_to_target(self) -> None:
        g: Graph[CounterState] = Graph()
        g.add_node("a", AddCommandNode(amount=1, goto="b"))
        g.add_node("b", AddAndDispatchNode(amount=10, target=GraphNode.END))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", "b")
        g.add_edge("b", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = make_parallel_ctx(CounterState(count=0))
        result = await GraphEngine(compiled).run_async(ctx)
        assert result.count == 11


class TestCommandGotoListTask:
    async def test_list_task_dispatches_all_in_parallel(self) -> None:
        """Command(goto=[Task(node="B"), Task(node="C")]) → B and C dispatched."""
        g: Graph[CounterState] = Graph()
        g.add_node(
            "a",
            AddCommandNode(
                amount=1,
                goto=[Task(node="b"), Task(node="c")],
            ),
        )
        g.add_node("b", AddAndDispatchNode(amount=10, target=GraphNode.END))
        g.add_node("c", AddAndDispatchNode(amount=100, target=GraphNode.END))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", "b")
        g.add_edge("a", "c")
        g.add_edge("b", GraphNode.END)
        g.add_edge("c", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = make_parallel_ctx(CounterState(count=0))
        result = await GraphEngine(compiled).run_async(ctx)
        # Fork isolation (Task 05): B, C on forked states — imperative
        # mutations don't propagate. Only A (fast path) persists.
        assert result.count == 1

    async def test_list_task_creates_instances_for_each(self) -> None:
        g: Graph[CounterState] = Graph()
        g.add_node(
            "a",
            AddCommandNode(
                amount=1,
                goto=[Task(node="b"), Task(node="c")],
            ),
        )
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
        await engine.run_async(ctx)

        scheduler = engine._scheduler
        assert isinstance(scheduler, ParallelScheduler)
        b_instances = [i for i in scheduler._instances.values() if i.node_name == "b"]
        c_instances = [i for i in scheduler._instances.values() if i.node_name == "c"]
        assert len(b_instances) == 1
        assert len(c_instances) == 1

    async def test_list_task_state_update_as_payload(self) -> None:
        """Command.goto dispatches carry state_update as payload."""
        g: Graph[CounterState] = Graph()
        g.add_node(
            "a",
            _StateUpdateCommandNode(
                amount=1,
                goto=[Task(node="b")],
                label="payload_data",
            ),
        )
        g.add_node("b", AddAndDispatchNode(amount=0, target=GraphNode.END))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", "b")
        g.add_edge("b", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = make_parallel_ctx(CounterState())
        engine = GraphEngine(compiled)
        await engine.run_async(ctx)

        scheduler = engine._scheduler
        assert isinstance(scheduler, ParallelScheduler)
        a_to_b = [
            e for e in scheduler._dispatch_log if e.source_instance == "a#0" and e.target == "b"
        ]
        assert len(a_to_b) == 1
        assert a_to_b[0].payload == {"messages": ["payload_data"]}


class _StateUpdateCommandNode(Node[CounterState]):
    """Returns Command(goto=...) + state_update."""

    def __init__(self, amount: int, goto: str | list[Task] | None, label: str) -> None:
        self.amount = amount
        self.goto = goto
        self.label = label

    def execute(self, ctx: GraphContext[CounterState]) -> NodeResult:
        ctx.state.count += self.amount
        return NodeResult(
            command=Command(goto=self.goto),
            state_update={"messages": [self.label]},
        )


# ── Mixed mode: manual dispatch + declarative transition ──────────────────


class TestMixedDispatchAndTransition:
    async def test_manual_dispatch_and_transition_both_fire(self) -> None:
        """Node dispatches D manually + returns transition matching E.

        Both D and E should be dispatched (not mutually exclusive).
        """
        g: Graph[CounterState] = Graph()
        g.add_node(
            "a",
            ManualDispatchPlusTransitionNode(
                manual_target="d",
                transition="done",
                amount=1,
            ),
        )
        g.add_node("d", AddAndDispatchNode(amount=10, target=GraphNode.END))
        g.add_node("e", AddAndDispatchNode(amount=100, target=GraphNode.END))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", "d")  # manual dispatch edge
        g.add_edge("a", "e", reason="done")  # transition edge
        g.add_edge("d", GraphNode.END)
        g.add_edge("e", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = make_parallel_ctx(CounterState(count=0))
        result = await GraphEngine(compiled).run_async(ctx)
        # Fork isolation (Task 05): D, E on forked states — imperative
        # mutations don't propagate. Only A (fast path) persists.
        assert result.count == 1

    async def test_mixed_mode_creates_both_instances(self) -> None:
        g: Graph[CounterState] = Graph()
        g.add_node(
            "a",
            ManualDispatchPlusTransitionNode(
                manual_target="d",
                transition="done",
                amount=1,
            ),
        )
        g.add_node("d", AddAndDispatchNode(amount=10, target=GraphNode.END))
        g.add_node("e", AddAndDispatchNode(amount=100, target=GraphNode.END))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", "d")
        g.add_edge("a", "e", reason="done")
        g.add_edge("d", GraphNode.END)
        g.add_edge("e", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = make_parallel_ctx(CounterState(count=0))
        engine = GraphEngine(compiled)
        await engine.run_async(ctx)

        scheduler = engine._scheduler
        assert isinstance(scheduler, ParallelScheduler)
        d_instances = [i for i in scheduler._instances.values() if i.node_name == "d"]
        e_instances = [i for i in scheduler._instances.values() if i.node_name == "e"]
        assert len(d_instances) == 1
        assert len(e_instances) == 1

    async def test_manual_dispatch_only_no_default_added(self) -> None:
        """Node manually dispatches but returns no transition/Command.

        The default edge should NOT auto-fire (node handled its own routing).
        """
        g: Graph[CounterState] = Graph()
        g.add_node("a", AddAndDispatchNode(amount=1, target=GraphNode.END))
        g.add_node("b", AddAndDispatchNode(amount=10, target=GraphNode.END))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", GraphNode.END)  # default edge — should NOT fire
        g.add_edge("a", "b", reason=None)  # another default — should NOT fire
        g.add_edge("b", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = make_parallel_ctx(CounterState(count=0))
        result = await GraphEngine(compiled).run_async(ctx)
        # Only A(1) ran — A manually dispatched to END, default edges skipped.
        assert result.count == 1


# ── Silent skip ───────────────────────────────────────────────────────────


class TestSilentSkip:
    async def test_no_dispatch_no_transition_no_error(self) -> None:
        """Node does nothing → silent skip, graph terminates, no error."""
        g: Graph[CounterState] = Graph()
        g.add_node("a", NoOpNode())
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", GraphNode.END, reason=None)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = make_parallel_ctx(CounterState(count=0))
        result = await GraphEngine(compiled).run_async(ctx)
        # A didn't dispatch, didn't return transition, but there IS a default
        # edge → default edge fires (dispatch to END). Graph terminates.
        assert result.count == 0

    async def test_no_dispatch_no_transition_no_default_silent_skip(self) -> None:
        """Node does nothing, no default edge → silent skip, graph terminates.

        The node has no outgoing edges to non-END targets and no default.
        Actually, the graph requires at least one edge from "a" for compile
        to succeed. So we give it an explicit-edge (reason="explicit") that
        doesn't match the (absent) transition. Since the node returns no
        transition and made no manual dispatch, the default edge fallback
        fires — but there's no default edge either, so silent skip.
        """
        g: Graph[CounterState] = Graph()
        g.add_node("a", NoOpNode())
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", GraphNode.END, reason="explicit")
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = make_parallel_ctx(CounterState(count=0))
        result = await GraphEngine(compiled).run_async(ctx)
        assert result.count == 0

    async def test_silent_skip_no_downstream_instances(self) -> None:
        g: Graph[CounterState] = Graph()
        g.add_node("a", NoOpNode())
        g.add_node("b", AddAndDispatchNode(amount=10, target=GraphNode.END))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", GraphNode.END, reason="explicit")
        g.add_edge("a", "b", reason="goto_b")
        g.add_edge("b", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = make_parallel_ctx(CounterState(count=0))
        engine = GraphEngine(compiled)
        await engine.run_async(ctx)

        scheduler = engine._scheduler
        assert isinstance(scheduler, ParallelScheduler)
        # Only "a" instance created; "b" never dispatched.
        b_instances = [i for i in scheduler._instances.values() if i.node_name == "b"]
        assert len(b_instances) == 0


# ── RoutingError ──────────────────────────────────────────────────────────


class TestRoutingErrorOnNoMatch:
    async def test_transition_no_match_no_default_raises(self) -> None:
        """transition with no matching edge and no default → RoutingError."""
        g: Graph[CounterState] = Graph()
        g.add_node("a", AddTransitionNode(amount=1, transition="nonexistent"))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", GraphNode.END, reason="other")
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = make_parallel_ctx(CounterState(count=0))
        with pytest.raises(RoutingError, match="nonexistent"):
            await GraphEngine(compiled).run_async(ctx)

    async def test_routing_error_message_mentions_node_and_transition(self) -> None:
        g: Graph[CounterState] = Graph()
        g.add_node("a", AddTransitionNode(amount=1, transition="bad"))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", GraphNode.END, reason="good")
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = make_parallel_ctx(CounterState(count=0))
        with pytest.raises(RoutingError, match="'a'"):
            await GraphEngine(compiled).run_async(ctx)


# ── Fan-out + fan-in end-to-end ───────────────────────────────────────────


class TestFanOutFanIn:
    """A → [B, C] → D → END. D uses ON_RECEIVE (explicit) so each dispatch
    creates a separate D instance. Task 06 changed the default to
    ON_ALL_PREDS; these tests pin ON_RECEIVE to preserve their original
    fan-out routing intent."""

    async def test_fanout_fanin_completes(self) -> None:
        g: Graph[CounterState] = Graph()
        g.add_node("a", AddTransitionNode(amount=1, transition="fan_out"))
        g.add_node("b", AddAndDispatchNode(amount=10, target="d"))
        g.add_node("c", AddAndDispatchNode(amount=100, target="d"))
        g.add_node("d", AddAndDispatchNode(amount=1000, target=GraphNode.END))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", "b", reason="fan_out")
        g.add_edge("a", "c", reason="fan_out")
        g.add_edge("b", "d")
        g.add_edge("c", "d")
        g.add_edge("d", GraphNode.END)
        compiled = g.compile(
            scheduler=SchedulerKind.PARALLEL,
            default_trigger=NodeTrigger.ON_RECEIVE,
        )

        ctx = make_parallel_ctx(CounterState(count=0))
        result = await GraphEngine(compiled).run_async(ctx)
        # Fork isolation (Task 05): B, C, D execute on forked states —
        # imperative mutations don't propagate. Only A (fast path) persists.
        # test_fanout_fanin_d_executes_twice verifies D ran twice.
        assert result.count == 1

    async def test_fanout_fanin_d_executes_twice(self) -> None:
        g: Graph[CounterState] = Graph()
        g.add_node("a", AddTransitionNode(amount=1, transition="fan_out"))
        g.add_node("b", AddAndDispatchNode(amount=10, target="d"))
        g.add_node("c", AddAndDispatchNode(amount=100, target="d"))
        g.add_node("d", AddAndDispatchNode(amount=1000, target=GraphNode.END))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", "b", reason="fan_out")
        g.add_edge("a", "c", reason="fan_out")
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
        assert len(d_instances) == 2

    async def test_fanout_via_command_goto_list_task_fanin(self) -> None:
        """Same fan-out + fan-in, but fan-out via Command.goto=[Task, Task]."""
        g: Graph[CounterState] = Graph()
        g.add_node(
            "a",
            AddCommandNode(
                amount=1,
                goto=[Task(node="b"), Task(node="c")],
            ),
        )
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
        result = await GraphEngine(compiled).run_async(ctx)
        # Fork isolation (Task 05): B, C, D on forked states — mutations
        # don't propagate. Only A (fast path) persists.
        assert result.count == 1
