"""Tests for ParallelScheduler, NodeInstance, and NodeInstanceStatus.

Covers the Task 03 acceptance criteria:

- `NodeInstanceStatus` StrEnum: DORMANT / PENDING / READY / RUNNING / COMPLETED.
- `NodeInstance` regular class with __slots__: instance_id, node_name, seq,
  status, upstream_payloads. instance_id format `{node_name}#{seq}`.
- `ParallelScheduler` execution loop: entry → execute → dispatch →
  downstream → ... → terminate when ready+active empty.
- `ctx.dispatch(target)`: validates target in outgoing edges,
  and takes effect immediately.
- `RoutingError` on invalid dispatch target.
- `max_iterations` per-instance-execution counting.
- `GraphContext.dispatch` works under both LINEAR and PARALLEL (both
  schedulers register a handler). Raises RuntimeError only if no handler.
- `GraphEngine._select_scheduler` returns ParallelScheduler for PARALLEL.
- No bare strings in framework code (all enums via StrEnum).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from helpers import CounterState, make_coordinator, make_ctx, make_runtime

from modex_graph import (
    DefaultGraphState,
    Graph,
    GraphContext,
    GraphEngine,
    GraphNode,
    GraphRecursionError,
    GraphRuntime,
    IntegratedInput,
    InvocationContext,
    LinearScheduler,
    Node,
    NodeInstance,
    NodeInstanceStatus,
    NodeTrigger,
    ParallelScheduler,
    RoutingError,
    Scheduler,
    SchedulerKind,
)
from modex_graph.scheduler.bootstrap import BootstrapMode

# ── Test helpers ──────────────────────────────────────────────────────────


class DispatchAddNode(Node[CounterState]):
    """Increments count by `amount`, then delivers to `target` if set."""

    def __init__(self, amount: int, target: str | None = None) -> None:
        self.amount = amount
        self.target = target

    async def execute(
        self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput
    ) -> None:
        ctx.state.count += self.amount
        if self.target is not None:
            self.deliver(None, self.target, ctx)
        return None


def make_parallel_ctx(state: CounterState | None = None) -> GraphContext[CounterState]:
    """Build a GraphContext configured for ParallelScheduler."""
    return GraphContext(
        state=state if state is not None else CounterState(),
        runtime=make_runtime(),
        coordinator=make_coordinator(),
        scheduler_kind=SchedulerKind.PARALLEL,
    )


# ── NodeInstanceStatus ────────────────────────────────────────────────────


class TestNodeInstanceStatus:
    def test_is_strenum(self) -> None:
        from enum import StrEnum

        assert issubclass(NodeInstanceStatus, StrEnum)

    def test_five_members(self) -> None:
        assert len(NodeInstanceStatus) == 5

    def test_values(self) -> None:
        assert NodeInstanceStatus.DORMANT == "dormant"
        assert NodeInstanceStatus.PENDING == "pending"
        assert NodeInstanceStatus.READY == "ready"
        assert NodeInstanceStatus.RUNNING == "running"
        assert NodeInstanceStatus.COMPLETED == "completed"

    def test_members_are_str(self) -> None:
        for member in NodeInstanceStatus:
            assert isinstance(member, str)


# ── NodeInstance ──────────────────────────────────────────────────────────


class TestNodeInstance:
    def test_regular_class_not_pydantic(self) -> None:
        from pydantic import BaseModel

        assert not issubclass(NodeInstance, BaseModel)

    def test_has_slots(self) -> None:
        assert hasattr(NodeInstance, "__slots__")
        expected = {
            "instance_id",
            "node_name",
            "seq",
            "status",
        }
        assert set(NodeInstance.__slots__) == expected

    def test_fields(self) -> None:
        instance = NodeInstance(
            instance_id="llm#0",
            node_name="llm",
            seq=0,
            status=NodeInstanceStatus.DORMANT,
        )
        assert instance.instance_id == "llm#0"
        assert instance.node_name == "llm"
        assert instance.seq == 0
        assert instance.status == NodeInstanceStatus.DORMANT

    def test_repr(self) -> None:
        instance = NodeInstance(
            instance_id="a#0",
            node_name="a",
            seq=0,
            status=NodeInstanceStatus.COMPLETED,
        )
        r = repr(instance)
        assert "a#0" in r
        assert "completed" in r


# ── ParallelScheduler inheritance ────────────────────────────────────────


class TestParallelSchedulerInheritance:
    def test_inherits_scheduler(self) -> None:
        assert issubclass(ParallelScheduler, Scheduler)

    def test_is_concrete(self) -> None:
        g: Graph[CounterState] = Graph()
        g.add_node("a", DispatchAddNode(amount=1, target=GraphNode.END))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)
        scheduler = ParallelScheduler(compiled)
        assert isinstance(scheduler, Scheduler)

    def test_no_abstract_methods(self) -> None:
        assert len(ParallelScheduler.__abstractmethods__) == 0


# ── GraphEngine._select_scheduler ────────────────────────────────────────


class TestGraphEngineSelectScheduler:
    def test_returns_parallel_scheduler_for_parallel(self) -> None:
        g: Graph[CounterState] = Graph()
        g.add_node("a", DispatchAddNode(amount=1, target=GraphNode.END))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)
        engine = GraphEngine(compiled)
        assert isinstance(engine._scheduler, ParallelScheduler)

    def test_returns_linear_scheduler_for_linear(self) -> None:
        from helpers import AddNode

        g: Graph[CounterState] = Graph()
        g.add_node("a", AddNode(amount=1))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.LINEAR)
        engine = GraphEngine(compiled)
        assert isinstance(engine._scheduler, LinearScheduler)


# ── Linear graph A→B→END under ParallelScheduler ─────────────────────────


class TestLinearGraphParallel:
    """Linear graph A→B→END executed via ParallelScheduler with manual dispatch."""

    async def test_linear_chain_a_b_end(self) -> None:
        g: Graph[CounterState] = Graph()
        g.add_node("a", DispatchAddNode(amount=1, target="b"))
        g.add_node("b", DispatchAddNode(amount=2, target=GraphNode.END))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", "b")
        g.add_edge("b", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = make_parallel_ctx(CounterState(count=0))
        result = await GraphEngine(compiled).run_async(ctx, mode=BootstrapMode.FRESH)
        assert result.count == 3

    async def test_sync_run(self) -> None:
        g: Graph[CounterState] = Graph()
        g.add_node("a", DispatchAddNode(amount=5, target=GraphNode.END))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = make_parallel_ctx(CounterState(count=0))
        result = GraphEngine(compiled).run(ctx, mode=BootstrapMode.FRESH)
        assert result.count == 5

    async def test_three_node_chain(self) -> None:
        g: Graph[CounterState] = Graph()
        g.add_node("a", DispatchAddNode(amount=1, target="b"))
        g.add_node("b", DispatchAddNode(amount=2, target="c"))
        g.add_node("c", DispatchAddNode(amount=3, target=GraphNode.END))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", "b")
        g.add_edge("b", "c")
        g.add_edge("c", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = make_parallel_ctx(CounterState(count=0))
        result = await GraphEngine(compiled).run_async(ctx, mode=BootstrapMode.FRESH)
        assert result.count == 6

    async def test_node_without_dispatch_terminates(self) -> None:
        """A node that delivers to END terminates the graph."""

        class NoDispatchNode(Node[CounterState]):
            async def execute(
                self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput
            ) -> None:
                ctx.state.count += 10
                self.deliver(None, None, ctx)
                return None

        g: Graph[CounterState] = Graph()
        g.add_node("a", NoDispatchNode())
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = make_parallel_ctx(CounterState(count=0))
        result = await GraphEngine(compiled).run_async(ctx, mode=BootstrapMode.FRESH)
        assert result.count == 10


# ── Single-node execution ────────────────────────────────────────────────


class TestSingleNodeExecution:
    """A single READY instance executes against shared state."""

    async def test_single_node_executes(self) -> None:
        g: Graph[CounterState] = Graph()
        g.add_node("a", DispatchAddNode(amount=7, target=GraphNode.END))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = make_parallel_ctx(CounterState(count=0))
        result = await GraphEngine(compiled).run_async(ctx, mode=BootstrapMode.FRESH)
        assert result.count == 7

    async def test_creates_start_work_and_end_instances(self) -> None:
        g: Graph[CounterState] = Graph()
        g.add_node("a", DispatchAddNode(amount=1, target=GraphNode.END))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = make_parallel_ctx(CounterState(count=0))
        engine = GraphEngine(compiled)
        await engine.run_async(ctx, mode=BootstrapMode.FRESH)

        scheduler = engine._scheduler
        assert isinstance(scheduler, ParallelScheduler)
        assert len(scheduler._instances) == 3
        instance = scheduler._instances["a#1"]
        assert instance.status == NodeInstanceStatus.COMPLETED

    async def test_iteration_count(self) -> None:
        """Single-node execution includes executable START and END."""
        g: Graph[CounterState] = Graph()
        g.add_node("a", DispatchAddNode(amount=1, target=GraphNode.END))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = make_parallel_ctx(CounterState(count=0))
        engine = GraphEngine(compiled)
        await engine.run_async(ctx, mode=BootstrapMode.FRESH)

        scheduler = engine._scheduler
        assert isinstance(scheduler, ParallelScheduler)
        assert scheduler._iteration_count == 3


# ── END dispatch ──────────────────────────────────────────────────────────


class TestEndDispatch:
    async def test_dispatch_to_end_creates_completed_instance(self) -> None:
        g: Graph[CounterState] = Graph()
        g.add_node("a", DispatchAddNode(amount=1, target=GraphNode.END))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = make_parallel_ctx(CounterState(count=0))
        engine = GraphEngine(compiled)
        await engine.run_async(ctx, mode=BootstrapMode.FRESH)

        scheduler = engine._scheduler
        assert isinstance(scheduler, ParallelScheduler)
        end_instances = [
            inst for inst in scheduler._instances.values() if inst.node_name == GraphNode.END
        ]
        assert len(end_instances) == 1
        assert end_instances[0].status == NodeInstanceStatus.COMPLETED


# ── RoutingError on invalid dispatch target ──────────────────────────────


class TestRoutingError:
    async def test_dispatch_to_non_edge_target_raises(self) -> None:
        """ctx.dispatch to a target not in outgoing edges raises RoutingError."""

        class BadDispatchNode(Node[CounterState]):
            async def execute(
                self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput
            ) -> None:
                ctx.dispatch("nonexistent")  # no edge from "a" to "nonexistent"
                return None

        g: Graph[CounterState] = Graph()
        g.add_node("a", BadDispatchNode())
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = make_parallel_ctx(CounterState(count=0))
        with pytest.raises(RoutingError, match="nonexistent"):
            await GraphEngine(compiled).run_async(ctx, mode=BootstrapMode.FRESH)

    async def test_dispatch_to_registered_node_without_edge_raises(self) -> None:
        """dispatch to a registered node with no edge from source raises.

        Graph: START→a, a→c, a→END, c→b, b→END. Node "a" tries to
        dispatch directly to "b" — but there's no edge a→b (only a→c
        and a→END). The intermediary "c" makes "b" reachable from
        START (satisfying PARALLEL reachability validation) without
        adding a direct a→b edge.
        """

        class DispatchToBNode(Node[CounterState]):
            async def execute(
                self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput
            ) -> None:
                ctx.dispatch("b")  # "b" exists but no direct edge a→b
                return None

        g: Graph[CounterState] = Graph()
        g.add_node("a", DispatchToBNode())
        g.add_node("b", DispatchAddNode(amount=1, target=GraphNode.END))
        g.add_node("c", DispatchAddNode(amount=0, target="b"))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", "c")
        g.add_edge("a", GraphNode.END)
        g.add_edge("c", "b")
        g.add_edge("b", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = make_parallel_ctx(CounterState(count=0))
        with pytest.raises(RoutingError, match="outgoing edges"):
            await GraphEngine(compiled).run_async(ctx, mode=BootstrapMode.FRESH)


# ── max_iterations in parallel mode ──────────────────────────────────────


class TestMaxIterations:
    async def test_self_dispatch_loop_raises_recursion_error(self) -> None:
        """A node that dispatches to itself loops until max_iterations."""

        class SelfDispatchNode(Node[CounterState]):
            async def execute(
                self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput
            ) -> None:
                ctx.state.count += 1
                self.deliver(None, "loop", ctx)
                return None

        g: Graph[CounterState] = Graph()
        g.add_node("loop", SelfDispatchNode())
        g.add_edge(GraphNode.START, "loop")
        g.add_edge("loop", "loop")
        g.add_edge("loop", GraphNode.END)
        compiled = g.compile(
            max_iterations=5,
            scheduler=SchedulerKind.PARALLEL,
            cycle_detection="off",
        )

        ctx = make_parallel_ctx(CounterState(count=0))
        with pytest.raises(GraphRecursionError, match="max_iterations=5"):
            await GraphEngine(compiled).run_async(ctx, mode=BootstrapMode.FRESH)
        assert ctx.state.count == 4

    async def test_iteration_count_matches_executions(self) -> None:
        g: Graph[CounterState] = Graph()
        g.add_node("a", DispatchAddNode(amount=1, target="b"))
        g.add_node("b", DispatchAddNode(amount=2, target=GraphNode.END))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", "b")
        g.add_edge("b", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = make_parallel_ctx(CounterState(count=0))
        engine = GraphEngine(compiled)
        await engine.run_async(ctx, mode=BootstrapMode.FRESH)

        scheduler = engine._scheduler
        assert isinstance(scheduler, ParallelScheduler)
        assert scheduler._iteration_count == 4
        assert len(scheduler._instances) == 4

    async def test_fanout_reserves_iterations_before_tasks_yield(self) -> None:
        class YieldingRuntime(GraphRuntime):
            def __init__(self) -> None:
                self.before_calls: list[str] = []

            async def before_node(self, ctx: GraphContext[Any], node_name: str) -> None:
                self.before_calls.append(node_name)
                await asyncio.sleep(0)

        class FanOutNode(Node[CounterState]):
            async def execute(
                self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput
            ) -> None:
                ctx.state.count += 1
                self.deliver(None, "b", ctx)
                self.deliver(None, "c", ctx)
                return None

        g: Graph[CounterState] = Graph()
        g.add_node("a", FanOutNode())
        g.add_node("b", DispatchAddNode(amount=10, target=GraphNode.END))
        g.add_node("c", DispatchAddNode(amount=100, target=GraphNode.END))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", "b")
        g.add_edge("a", "c")
        g.add_edge("b", GraphNode.END)
        g.add_edge("c", GraphNode.END)
        compiled = g.compile(max_iterations=2, scheduler=SchedulerKind.PARALLEL)
        runtime = YieldingRuntime()
        ctx = GraphContext(
            state=CounterState(),
            runtime=runtime,
            coordinator=make_coordinator(),
            scheduler_kind=SchedulerKind.PARALLEL,
        )
        scheduler = ParallelScheduler(compiled)

        with pytest.raises(GraphRecursionError, match="max_iterations=2"):
            await scheduler.run_async(ctx, mode=BootstrapMode.FRESH)

        assert scheduler._iteration_count == compiled.max_iterations
        assert set(runtime.before_calls) == {GraphNode.START, "a"}


class TestExecutionContextShell:
    async def test_before_node_does_not_observe_parent_invocation(self) -> None:
        from modex_graph.execution_context import get_execution

        class InvocationCapturingRuntime(GraphRuntime):
            def __init__(self) -> None:
                self.before_invocations: list[InvocationContext | None] = []

            async def before_node(self, ctx: GraphContext[Any], node_name: str) -> None:
                exec_ctx = get_execution()
                self.before_invocations.append(
                    exec_ctx.invocation if exec_ctx is not None else None
                )

        g: Graph[CounterState] = Graph()
        g.add_node("a", DispatchAddNode(amount=1, target=GraphNode.END))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)
        runtime = InvocationCapturingRuntime()
        ctx = GraphContext(
            state=CounterState(),
            runtime=runtime,
            coordinator=make_coordinator(),
            scheduler_kind=SchedulerKind.PARALLEL,
        )

        await ParallelScheduler(compiled).run_async(ctx, mode=BootstrapMode.FRESH)

        assert runtime.before_invocations == [None, None, None]


# ── LinearScheduler dispatch works under LINEAR ──────────────────────────


class TestLinearDispatchWorks:
    """LinearScheduler registers a dispatch handler — ctx.dispatch works
    under LINEAR (rule 15 convergence: no scheduler_kind branch)."""

    async def test_linear_scheduler_executes_normally(self) -> None:
        """LinearScheduler still works for graphs without explicit dispatch calls."""
        from helpers import AddNode

        g: Graph[CounterState] = Graph()
        g.add_node("a", AddNode(amount=3))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.LINEAR)

        ctx = make_ctx(CounterState(count=0))
        result = await GraphEngine(compiled).run_async(ctx, mode=BootstrapMode.FRESH)
        assert result.count == 3


# ── GraphContext dispatch handler ─────────────────────────────────────────


class TestGraphContextDispatch:
    def test_parallel_without_handler_raises_runtime_error(self) -> None:
        """PARALLEL context with handler explicitly cleared is a programmer error."""
        ctx = GraphContext(
            state=CounterState(),
            runtime=make_runtime(),
            coordinator=make_coordinator(),
            scheduler_kind=SchedulerKind.PARALLEL,
        )
        ctx.set_dispatch_handler(None)  # explicitly clear the default no-op
        with pytest.raises(RuntimeError, match="no dispatch_handler"):
            ctx.dispatch("target")

    def test_set_dispatch_handler(self) -> None:
        from modex_graph.execution_context import NodeExecution, reset_execution, set_execution

        calls: list[tuple[str, str]] = []

        def handler(source: str, target: str) -> None:
            calls.append((source, target))

        ctx = GraphContext(
            state=CounterState(),
            runtime=make_runtime(),
            coordinator=make_coordinator(),
            scheduler_kind=SchedulerKind.PARALLEL,
            dispatch_handler=handler,
        )
        exec_ctx = NodeExecution(instance_id="a#0")
        token = set_execution(exec_ctx)
        try:
            ctx.dispatch("b")
        finally:
            reset_execution(token)
        assert calls == [("a#0", "b")]


# ── Architecture guard ────────────────────────────────────────────────────


class TestArchitectureGuard:
    def test_no_bare_strings_for_status(self) -> None:
        """NodeInstanceStatus values used, not bare strings."""
        assert NodeInstanceStatus.DORMANT == "dormant"
        assert isinstance(NodeInstanceStatus.DORMANT, str)
        assert isinstance(NodeInstanceStatus.RUNNING, str)

    def test_parallel_scheduler_inherits_scheduler(self) -> None:
        assert issubclass(ParallelScheduler, Scheduler)

    def test_node_instance_status_in_constants_module(self) -> None:
        from modex_graph.constants import NodeInstanceStatus as ConstantsStatus

        assert ConstantsStatus is NodeInstanceStatus

    def test_public_exports(self) -> None:
        import modex_graph

        for name in (
            "ParallelScheduler",
            "NodeInstanceStatus",
            "NodeInstance",
        ):
            assert name in modex_graph.__all__, f"{name} not in __all__"
            assert hasattr(modex_graph, name), f"{name} not importable"


# ── Routing compilation integration ─────────────────────────────────────


class TestRoutingCompilationIntegration:
    """Integration tests for deliver-based dispatch under ParallelScheduler."""

    async def test_default_edge_fires_when_no_explicit_target(self) -> None:
        """Node delivers with next_node=None → default edge fires."""

        class DeliverDefaultNode(Node[CounterState]):
            async def execute(
                self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput
            ) -> None:
                ctx.state.count += 1
                self.deliver(None, None, ctx)
                return None

        g: Graph[CounterState] = Graph()
        g.add_node("a", DeliverDefaultNode())
        g.add_node("b", DispatchAddNode(amount=10, target=GraphNode.END))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", "b")
        g.add_edge("b", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = make_parallel_ctx(CounterState(count=0))
        result = await GraphEngine(compiled).run_async(ctx, mode=BootstrapMode.FRESH)
        assert result.count == 11


# ── END trigger forced to ON_ALL_PREDS (bug C2) ───────────────────────────


class TestEndNodeForcedOnAllPreds:
    """END must always use ON_ALL_PREDS, even when default_trigger=ON_RECEIVE.

    Regression for bug C2: with ON_RECEIVE, two parallel predecessors
    delivering to END spawned multiple END instances; the last received
    empty input and overwrote state.result with []. Forcing ON_ALL_PREDS
    collapses them into a single execution aggregating both payloads.
    """

    async def test_end_executes_once_under_on_receive_default(self) -> None:
        class FanOutNode(Node[DefaultGraphState]):
            async def execute(
                self,
                ctx: GraphContext[DefaultGraphState],
                integrated_input: IntegratedInput,
            ) -> None:
                self.deliver(None, "b", ctx)
                self.deliver(None, "c", ctx)

        class DeliverContentNode(Node[DefaultGraphState]):
            def __init__(self, content: str) -> None:
                self.content = content

            async def execute(
                self,
                ctx: GraphContext[DefaultGraphState],
                integrated_input: IntegratedInput,
            ) -> None:
                self.deliver(self.content, GraphNode.END, ctx)

        g: Graph[DefaultGraphState] = Graph()
        g.add_node("a", FanOutNode())
        g.add_node("b", DeliverContentNode("payload-b"))
        g.add_node("c", DeliverContentNode("payload-c"))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", "b")
        g.add_edge("a", "c")
        g.add_edge("b", GraphNode.END)
        g.add_edge("c", GraphNode.END)
        compiled = g.compile(
            scheduler=SchedulerKind.PARALLEL,
            default_trigger=NodeTrigger.ON_RECEIVE,
        )

        ctx = GraphContext(
            state=DefaultGraphState(),
            runtime=make_runtime(),
            coordinator=make_coordinator(),
            scheduler_kind=SchedulerKind.PARALLEL,
        )
        engine = GraphEngine(compiled)
        result = await engine.run_async(ctx, mode=BootstrapMode.FRESH)

        scheduler = engine._scheduler
        assert isinstance(scheduler, ParallelScheduler)
        end_instances = [
            inst for inst in scheduler._instances.values() if inst.node_name == GraphNode.END
        ]
        assert len(end_instances) == 1
        assert end_instances[0].status == NodeInstanceStatus.COMPLETED

        assert result.result is not None
        assert len(result.result) == 2
        assert {p.content for p in result.result} == {"payload-b", "payload-c"}
