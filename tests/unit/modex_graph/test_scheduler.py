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
- GraphContext.dispatch: works under both LINEAR and PARALLEL (both
  schedulers register a dispatch handler). Raises RuntimeError if no
  handler is registered.
- GraphContext.scheduler_kind: defaults to LINEAR.
- Architecture guard: Scheduler is ABC (not Protocol), LinearScheduler
  inherits Scheduler.
- Zero behaviour change: Graph.compile(scheduler=LINEAR) == Graph.compile().
"""

from __future__ import annotations

import inspect
from abc import ABC

import pytest
from helpers import AddNode, AsyncAddNode, CounterState, make_coordinator, make_ctx

from modex_graph import (
    Graph,
    GraphContext,
    GraphEngine,
    GraphNode,
    IntegratedInput,
    LinearScheduler,
    Node,
    RoutingError,
    Scheduler,
    SchedulerKind,
)
from modex_graph.scheduler.bootstrap import BootstrapMode

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
        g.add_edge("a", "b")
        g.add_edge("b", GraphNode.END)

        compiled = g.compile()
        ctx = make_ctx(CounterState(count=0))
        scheduler = LinearScheduler(compiled)
        result = await scheduler.run_async(ctx, mode=BootstrapMode.FRESH)
        assert result.count == 3

    async def test_sync_run(self) -> None:
        g: Graph[CounterState] = Graph()
        g.add_node("a", AddNode(amount=5))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", GraphNode.END)
        compiled = g.compile()
        ctx = make_ctx(CounterState(count=0))
        scheduler = LinearScheduler(compiled)
        result = scheduler.run(ctx, mode=BootstrapMode.FRESH)
        assert result.count == 5

    async def test_async_node(self) -> None:
        g: Graph[CounterState] = Graph()
        g.add_node("a", AsyncAddNode(amount=8))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", GraphNode.END)
        compiled = g.compile()
        ctx = make_ctx(CounterState(count=0))
        scheduler = LinearScheduler(compiled)
        result = await scheduler.run_async(ctx, mode=BootstrapMode.FRESH)
        assert result.count == 8

    async def test_max_iterations_raises(self) -> None:
        class InfiniteNode(Node[CounterState]):
            async def execute(
                self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput
            ) -> None:
                ctx.state.count += 1
                self.deliver(None, "inf", ctx)
                return None

        g: Graph[CounterState] = Graph()
        g.add_node("inf", InfiniteNode())
        g.add_edge(GraphNode.START, "inf")
        g.add_edge("inf", "inf")
        compiled = g.compile(max_iterations=5)
        ctx = make_ctx(CounterState(count=0))
        scheduler = LinearScheduler(compiled)
        from modex_graph import GraphRecursionError

        with pytest.raises(GraphRecursionError, match="max_iterations=5"):
            await scheduler.run_async(ctx, mode=BootstrapMode.FRESH)


class TestLinearSchedulerTopologyEnforcement:
    """G3: LinearScheduler validates deliver targets against declared edges.

    Mirrors ParallelScheduler._handle_dispatch validation (parallel.py:460-466).
    """

    async def test_rejects_deliver_to_non_downstream_node(self) -> None:
        class BadDeliverNode(Node[CounterState]):
            async def execute(
                self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput
            ) -> None:
                self.deliver(None, "b", ctx)
                return None

        g: Graph[CounterState] = Graph()
        g.add_node("a", BadDeliverNode())
        g.add_node("c", AddNode(amount=1))
        g.add_node("b", AddNode(amount=1))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", "c")
        g.add_edge("c", "b")
        g.add_edge("b", GraphNode.END)
        compiled = g.compile()
        ctx = make_ctx(CounterState(count=0))
        scheduler = LinearScheduler(compiled)

        with pytest.raises(RoutingError, match="not in the outgoing edges"):
            await scheduler.run_async(ctx, mode=BootstrapMode.FRESH)

    async def test_allows_deliver_to_downstream_node(self) -> None:
        class GoodDeliverNode(Node[CounterState]):
            async def execute(
                self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput
            ) -> None:
                ctx.state.count += 1
                self.deliver(None, "c", ctx)
                return None

        g: Graph[CounterState] = Graph()
        g.add_node("a", GoodDeliverNode())
        g.add_node("c", AddNode(amount=1))
        g.add_node("b", AddNode(amount=1))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", "c")
        g.add_edge("c", "b")
        g.add_edge("b", GraphNode.END)
        compiled = g.compile()
        ctx = make_ctx(CounterState(count=0))
        scheduler = LinearScheduler(compiled)

        result = await scheduler.run_async(ctx, mode=BootstrapMode.FRESH)
        assert result.count == 3


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
        result = await engine.run_async(ctx, mode=BootstrapMode.FRESH)
        assert result.count == 7

    def test_engine_run_delegates(self) -> None:
        g: Graph[CounterState] = Graph()
        g.add_node("a", AddNode(amount=9))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", GraphNode.END)
        compiled = g.compile()
        ctx = make_ctx(CounterState(count=0))
        engine = GraphEngine(compiled)
        result = engine.run(ctx, mode=BootstrapMode.FRESH)
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
        r1 = await GraphEngine(default_compiled).run_async(ctx1, mode=BootstrapMode.FRESH)
        r2 = await GraphEngine(linear_compiled).run_async(ctx2, mode=BootstrapMode.FRESH)
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
        result = await GraphEngine(compiled).run_async(ctx, mode=BootstrapMode.FRESH)
        assert result.count == 1


# ── GraphContext.dispatch ─────────────────────────────────────────────────


class TestGraphContextDispatch:
    def test_dispatch_works_under_linear_with_handler(self) -> None:
        """dispatch works under LINEAR when a handler is registered."""
        calls: list[tuple[str, str]] = []

        def handler(src: str, tgt: str) -> None:
            calls.append((src, tgt))

        ctx = GraphContext(
            state=CounterState(),
            runtime=make_ctx(CounterState()).runtime,
            coordinator=make_coordinator(),
            scheduler_kind=SchedulerKind.LINEAR,
            dispatch_handler=handler,
        )
        ctx.dispatch("some_target")
        assert len(calls) == 1
        assert calls[0] == ("", "some_target")

    def test_dispatch_works_under_default_linear_with_handler(self) -> None:
        """dispatch works under default LINEAR (make_ctx provides a no-op handler)."""
        ctx = make_ctx(CounterState())
        assert ctx.scheduler_kind == SchedulerKind.LINEAR
        # Should NOT raise — make_ctx registers a no-op handler.
        ctx.dispatch("some_target")

    def test_dispatch_raises_without_handler_under_parallel(self) -> None:
        ctx = GraphContext(
            state=CounterState(),
            runtime=make_ctx(CounterState()).runtime,
            coordinator=make_coordinator(),
            scheduler_kind=SchedulerKind.PARALLEL,
        )
        ctx.set_dispatch_handler(None)  # explicitly clear the default no-op
        with pytest.raises(RuntimeError, match="no dispatch_handler"):
            ctx.dispatch("some_target")

    def test_dispatch_raises_without_handler_under_linear(self) -> None:
        """LINEAR without a handler is still a programmer error."""
        ctx = GraphContext(
            state=CounterState(),
            runtime=make_ctx(CounterState()).runtime,
            coordinator=make_coordinator(),
            scheduler_kind=SchedulerKind.LINEAR,
        )
        ctx.set_dispatch_handler(None)  # explicitly clear the default no-op
        with pytest.raises(RuntimeError, match="no dispatch_handler"):
            ctx.dispatch("some_target")

    def test_scheduler_kind_defaults_to_linear(self) -> None:
        ctx = make_ctx(CounterState())
        assert ctx.scheduler_kind == SchedulerKind.LINEAR


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
