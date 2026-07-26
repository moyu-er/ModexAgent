"""Tests for Scheduler ABC, LinearScheduler, SchedulerKind, and GraphEngine delegation.

Covers:
- SchedulerKind StrEnum values + membership.
- Scheduler ABC: abstract, cannot instantiate, declares run_async
  (abstract) + run (concrete, inherited by subclasses).
- LinearScheduler: inherits Scheduler, executes graphs identically to the
  pre-extraction GraphEngine.
- GraphEngine delegation: selects LinearScheduler under LINEAR,
  ParallelScheduler under PARALLEL.
- CompiledGraph.scheduler field defaults to LINEAR.
- Graph.compile(scheduler=...) parameter.
- GraphContext.dispatch: raises RuntimeError under LINEAR.
- GraphContext.scheduler_kind: defaults to LINEAR, propagates via fork.
- Architecture guard: Scheduler is ABC (not Protocol), LinearScheduler
  inherits Scheduler.
- Zero behaviour change: Graph.compile(scheduler=LINEAR) == Graph.compile().
"""
from __future__ import annotations

import inspect
from abc import ABC
from typing import Any

import pytest
from helpers import AddNode, AsyncAddNode, CounterState, make_ctx

from modex_graph import (
    Command,
    Graph,
    GraphContext,
    GraphEngine,
    GraphNode,
    LinearScheduler,
    Node,
    NodeResult,
    Scheduler,
    SchedulerKind,
    Task,
)

# ── SchedulerKind ─────────────────────────────────────────────────────────


class TestSchedulerKind:
    def test_linear_value(self) -> None:
        assert SchedulerKind.LINEAR == "linear"

    def test_parallel_value(self) -> None:
        assert SchedulerKind.PARALLEL == "parallel"

    def test_is_strenum(self) -> None:
        from enum import StrEnum

        assert issubclass(SchedulerKind, StrEnum)

    def test_members_are_str(self) -> None:
        assert isinstance(SchedulerKind.LINEAR, str)
        assert isinstance(SchedulerKind.PARALLEL, str)

    def test_two_members(self) -> None:
        assert len(SchedulerKind) == 2


# ── Scheduler ABC ─────────────────────────────────────────────────────────


class TestSchedulerABC:
    def test_scheduler_is_abc(self) -> None:
        assert issubclass(Scheduler, ABC)

    def test_scheduler_is_not_protocol(self) -> None:
        from typing import Protocol

        assert not issubclass(Scheduler, Protocol)

    def test_cannot_instantiate_scheduler(self) -> None:
        with pytest.raises(TypeError):
            Scheduler()  # type: ignore[abstract]

    def test_has_run_async_abstract(self) -> None:
        assert "run_async" in Scheduler.__abstractmethods__

    def test_run_is_concrete_on_abc(self) -> None:
        # `run` is a concrete method on the ABC (not abstract); subclasses
        # inherit it and only override `run_async`.
        assert "run" not in Scheduler.__abstractmethods__

    def test_run_inherited_not_overridden(self) -> None:
        from modex_graph import ParallelScheduler

        # Both subclasses inherit `run` from the ABC verbatim — no duplicate
        # overrides (Issue 1: deduplicated run()).
        assert LinearScheduler.run is Scheduler.run
        assert ParallelScheduler.run is Scheduler.run

    def test_run_async_signature(self) -> None:
        sig = inspect.signature(Scheduler.run_async)
        params = list(sig.parameters.keys())
        assert "ctx" in params

    def test_run_signature(self) -> None:
        sig = inspect.signature(Scheduler.run)
        params = list(sig.parameters.keys())
        assert "ctx" in params


# ── LinearScheduler ──────────────────────────────────────────────────────


class TestLinearSchedulerInheritance:
    def test_linear_scheduler_inherits_scheduler(self) -> None:
        assert issubclass(LinearScheduler, Scheduler)

    def test_linear_scheduler_is_concrete(self) -> None:
        g: Graph[CounterState] = Graph()
        g.add_node("a", AddNode(amount=1))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", GraphNode.END)
        compiled = g.compile()

        scheduler = LinearScheduler(compiled)
        assert isinstance(scheduler, Scheduler)

    def test_no_abstract_methods_remaining(self) -> None:
        assert len(LinearScheduler.__abstractmethods__) == 0


class TestLinearSchedulerExecution:
    """LinearScheduler executes graphs identically to the original GraphEngine."""

    async def test_linear_chain(self) -> None:
        g: Graph[CounterState] = Graph()
        g.add_node("a", AddNode(amount=1))
        g.add_node("b", AddNode(amount=2))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", "b", reason=None)
        g.add_edge("b", GraphNode.END, reason=None)

        compiled = g.compile()
        ctx = make_ctx(CounterState(count=0))
        scheduler = LinearScheduler(compiled)
        result = await scheduler.run_async(ctx)
        assert result.count == 3

    async def test_sync_run(self) -> None:
        g: Graph[CounterState] = Graph()
        g.add_node("a", AddNode(amount=5))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", GraphNode.END, reason=None)
        compiled = g.compile()
        ctx = make_ctx(CounterState(count=0))
        scheduler = LinearScheduler(compiled)
        result = scheduler.run(ctx)
        assert result.count == 5

    async def test_async_node(self) -> None:
        g: Graph[CounterState] = Graph()
        g.add_node("a", AsyncAddNode(amount=8))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", GraphNode.END)
        compiled = g.compile()
        ctx = make_ctx(CounterState(count=0))
        scheduler = LinearScheduler(compiled)
        result = await scheduler.run_async(ctx)
        assert result.count == 8

    async def test_command_goto_str(self) -> None:
        class GotoNode(Node[CounterState]):
            def execute(self, ctx: GraphContext[CounterState]) -> NodeResult:
                return NodeResult(command=Command(goto="target"))

        g: Graph[CounterState] = Graph()
        g.add_node("start", GotoNode())
        g.add_node("target", AddNode(amount=10))
        g.add_edge(GraphNode.START, "start")
        g.add_edge("target", GraphNode.END)
        compiled = g.compile()
        ctx = make_ctx(CounterState(count=0))
        scheduler = LinearScheduler(compiled)
        result = await scheduler.run_async(ctx)
        assert result.count == 10

    async def test_command_goto_list_task(self) -> None:
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
        scheduler = LinearScheduler(compiled)
        result = await scheduler.run_async(ctx)
        assert result.messages == ["w1", "w2"]

    async def test_transition_routing(self) -> None:
        class DecideNode(Node[CounterState]):
            def execute(self, ctx: GraphContext[CounterState]) -> NodeResult:
                return NodeResult(transition="high" if ctx.state.count > 5 else "low")

        g: Graph[CounterState] = Graph()
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
        scheduler = LinearScheduler(compiled)
        result = await scheduler.run_async(ctx)
        assert result.count == 1

    async def test_max_iterations_raises(self) -> None:
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
        scheduler = LinearScheduler(compiled)
        from modex_graph import GraphRecursionError

        with pytest.raises(GraphRecursionError, match="max_iterations=5"):
            await scheduler.run_async(ctx)


class _CommandNode(Node[CounterState]):
    def __init__(self, goto: Any) -> None:  # noqa: ANN401
        self.goto = goto

    def execute(self, ctx: GraphContext[CounterState]) -> NodeResult:
        return NodeResult(command=Command(goto=self.goto))


# ── GraphEngine delegation ────────────────────────────────────────────────


class TestGraphEngineDelegation:
    def test_engine_constructs_linear_scheduler_by_default(self) -> None:
        g: Graph[CounterState] = Graph()
        g.add_node("a", AddNode(amount=1))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", GraphNode.END)
        compiled = g.compile()
        engine = GraphEngine(compiled)
        assert isinstance(engine._scheduler, LinearScheduler)

    def test_engine_constructs_parallel_scheduler(self) -> None:
        g: Graph[CounterState] = Graph()
        g.add_node("a", AddNode(amount=1))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)
        engine = GraphEngine(compiled)
        from modex_graph import ParallelScheduler

        assert isinstance(engine._scheduler, ParallelScheduler)

    async def test_engine_run_async_delegates(self) -> None:
        g: Graph[CounterState] = Graph()
        g.add_node("a", AddNode(amount=7))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", GraphNode.END)
        compiled = g.compile()
        ctx = make_ctx(CounterState(count=0))
        engine = GraphEngine(compiled)
        result = await engine.run_async(ctx)
        assert result.count == 7

    def test_engine_run_delegates(self) -> None:
        g: Graph[CounterState] = Graph()
        g.add_node("a", AddNode(amount=9))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", GraphNode.END)
        compiled = g.compile()
        ctx = make_ctx(CounterState(count=0))
        engine = GraphEngine(compiled)
        result = engine.run(ctx)
        assert result.count == 9


# ── CompiledGraph.scheduler field ─────────────────────────────────────────


class TestCompiledGraphSchedulerField:
    def test_default_is_linear(self) -> None:
        g: Graph[CounterState] = Graph()
        g.add_node("a", AddNode(amount=1))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", GraphNode.END)
        compiled = g.compile()
        assert compiled.scheduler == SchedulerKind.LINEAR

    def test_explicit_linear(self) -> None:
        g: Graph[CounterState] = Graph()
        g.add_node("a", AddNode(amount=1))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.LINEAR)
        assert compiled.scheduler == SchedulerKind.LINEAR

    def test_explicit_parallel(self) -> None:
        g: Graph[CounterState] = Graph()
        g.add_node("a", AddNode(amount=1))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)
        assert compiled.scheduler == SchedulerKind.PARALLEL


# ── Graph.compile(scheduler=...) zero behaviour change ────────────────────


class TestCompileSchedulerParameter:
    """Graph.compile(scheduler=SchedulerKind.LINEAR) == Graph.compile()."""

    async def test_explicit_linear_same_as_default(self) -> None:
        g1: Graph[CounterState] = Graph()
        g1.add_node("a", AddNode(amount=3))
        g1.add_node("b", AddNode(amount=4))
        g1.add_edge(GraphNode.START, "a")
        g1.add_edge("a", "b")
        g1.add_edge("b", GraphNode.END)
        default_compiled = g1.compile()
        linear_compiled = g1.compile(scheduler=SchedulerKind.LINEAR)

        assert default_compiled.scheduler == linear_compiled.scheduler

        ctx1 = make_ctx(CounterState(count=0))
        ctx2 = make_ctx(CounterState(count=0))
        r1 = await GraphEngine(default_compiled).run_async(ctx1)
        r2 = await GraphEngine(linear_compiled).run_async(ctx2)
        assert r1.count == r2.count == 7

    async def test_compile_with_max_iterations_and_scheduler(self) -> None:
        g: Graph[CounterState] = Graph()
        g.add_node("a", AddNode(amount=1))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", GraphNode.END)
        compiled = g.compile(max_iterations=50, scheduler=SchedulerKind.LINEAR)
        assert compiled.max_iterations == 50
        assert compiled.scheduler == SchedulerKind.LINEAR

        ctx = make_ctx(CounterState(count=0))
        result = await GraphEngine(compiled).run_async(ctx)
        assert result.count == 1


# ── GraphContext.dispatch ─────────────────────────────────────────────────


class TestGraphContextDispatch:
    def test_dispatch_raises_under_default_linear(self) -> None:
        ctx = make_ctx(CounterState())
        with pytest.raises(RuntimeError, match="dispatch is only available under ParallelScheduler"):
            ctx.dispatch("some_target")

    def test_dispatch_raises_under_explicit_linear(self) -> None:
        ctx = GraphContext(
            state=CounterState(),
            runtime=make_ctx(CounterState()).runtime,
            scheduler_kind=SchedulerKind.LINEAR,
        )
        with pytest.raises(RuntimeError, match="dispatch is only available under ParallelScheduler"):
            ctx.dispatch("some_target")

    def test_dispatch_raises_without_handler_under_parallel(self) -> None:
        ctx = GraphContext(
            state=CounterState(),
            runtime=make_ctx(CounterState()).runtime,
            scheduler_kind=SchedulerKind.PARALLEL,
        )
        with pytest.raises(RuntimeError, match="no dispatch_handler"):
            ctx.dispatch("some_target")

    def test_scheduler_kind_defaults_to_linear(self) -> None:
        ctx = make_ctx(CounterState())
        assert ctx.scheduler_kind == SchedulerKind.LINEAR

    def test_scheduler_kind_propagates_through_fork(self) -> None:
        ctx = GraphContext(
            state=CounterState(),
            runtime=make_ctx(CounterState()).runtime,
            scheduler_kind=SchedulerKind.PARALLEL,
        )
        sub = ctx.fork(state=CounterState())
        assert sub.scheduler_kind == SchedulerKind.PARALLEL

    def test_scheduler_kind_inherited_through_fork_default(self) -> None:
        ctx = make_ctx(CounterState())
        sub = ctx.fork(state=CounterState())
        assert sub.scheduler_kind == SchedulerKind.LINEAR

    def test_fork_overrides_scheduler_kind(self) -> None:
        ctx = make_ctx(CounterState())
        sub = ctx.fork(state=CounterState(), scheduler_kind=SchedulerKind.PARALLEL)
        assert sub.scheduler_kind == SchedulerKind.PARALLEL


# ── Architecture guard ────────────────────────────────────────────────────


class TestArchitectureGuard:
    def test_scheduler_is_abc_not_protocol(self) -> None:
        from abc import ABC
        from typing import Protocol

        assert issubclass(Scheduler, ABC)
        assert not issubclass(Scheduler, Protocol)

    def test_linear_scheduler_inherits_scheduler(self) -> None:
        assert issubclass(LinearScheduler, Scheduler)

    def test_parallel_scheduler_inherits_scheduler(self) -> None:
        from modex_graph import ParallelScheduler

        assert issubclass(ParallelScheduler, Scheduler)

    def test_scheduler_kind_in_constants_module(self) -> None:
        from modex_graph.constants import SchedulerKind as ConstantsSchedulerKind

        assert ConstantsSchedulerKind is SchedulerKind

    def test_scheduler_module_exports(self) -> None:
        import modex_graph

        assert "Scheduler" in modex_graph.__all__
        assert "LinearScheduler" in modex_graph.__all__
        assert "ParallelScheduler" in modex_graph.__all__
        assert "SchedulerKind" in modex_graph.__all__
