"""Tests for ParallelScheduler, NodeInstance, NodeInstanceStatus, DispatchEvent.

Covers the Task 03 acceptance criteria:

- `NodeInstanceStatus` StrEnum: DORMANT / PENDING / READY / RUNNING / COMPLETED.
- `NodeInstance` regular class with __slots__: instance_id, node_name, seq,
  status, forked_state, fork_version, upstream_payloads. instance_id format
  `{node_name}#{seq}`.
- `DispatchEvent` frozen Pydantic model (extra="forbid"): source_instance,
  target, payload.
- `ParallelScheduler` execution loop: entry → execute → dispatch →
  downstream → ... → terminate when ready+active empty.
- Single-node fast path: one READY + no RUNNING → direct main_state.
- `ctx.dispatch(target, state_update)`: validates target in outgoing edges,
  creates DispatchEvent, takes effect immediately.
- `RoutingError` on invalid dispatch target.
- `max_iterations` per-instance-execution counting.
- `GraphContext.dispatch` works under both LINEAR and PARALLEL (both
  schedulers register a handler). Raises RuntimeError only if no handler.
- `GraphEngine._select_scheduler` returns ParallelScheduler for PARALLEL.
- No bare strings in framework code (all enums via StrEnum).
"""

from __future__ import annotations

from typing import Any

import pytest
from helpers import CounterState, make_ctx, make_runtime, make_coordinator
from pydantic import ValidationError

from modex_graph import (
    DispatchEvent,
    Graph,
    GraphContext,
    GraphEngine,
    GraphNode,
    GraphRecursionError,
    IntegratedInput,
    LinearScheduler,
    Node,
    NodeInstance,
    NodeInstanceStatus,
    NodeResult,
    ParallelScheduler,
    RoutingError,
    Scheduler,
    SchedulerKind,
)

# ── Test helpers ──────────────────────────────────────────────────────────


class DispatchAddNode(Node[CounterState]):
    """Increments count by `amount`, then delivers to `target` if set."""

    def __init__(self, amount: int, target: str | None = None) -> None:
        self.amount = amount
        self.target = target

    def execute(self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput) -> NodeResult:
        ctx.state.count += self.amount
        if self.target is not None:
            self.deliver(None, self.target, ctx)
        return NodeResult()


class DispatchAddWithPayloadNode(Node[CounterState]):
    """Increments count, delivers `payload` content to `target`."""

    def __init__(self, amount: int, target: str, payload: dict[str, Any]) -> None:
        self.amount = amount
        self.target = target
        self.payload = payload

    def execute(self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput) -> NodeResult:
        ctx.state.count += self.amount
        self.deliver(self.payload, self.target, ctx)
        return NodeResult()


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
            "forked_state",
            "fork_version",
            "upstream_payloads",
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
        assert instance.forked_state is None

    def test_forked_state_can_be_set(self) -> None:
        state = CounterState(count=42)
        instance = NodeInstance(
            instance_id="tool#1",
            node_name="tool",
            seq=1,
            status=NodeInstanceStatus.READY,
            forked_state=state,
        )
        assert instance.forked_state is state

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


# ── DispatchEvent ─────────────────────────────────────────────────────────


class TestDispatchEvent:
    def test_is_pydantic_model(self) -> None:
        from pydantic import BaseModel

        assert issubclass(DispatchEvent, BaseModel)

    def test_frozen(self) -> None:
        assert DispatchEvent.model_config.get("frozen") is True

    def test_extra_forbid(self) -> None:
        assert DispatchEvent.model_config.get("extra") == "forbid"

    def test_fields(self) -> None:
        event = DispatchEvent(
            source_instance="a#0",
            target="b",
            payload={"key": "value"},
        )
        assert event.source_instance == "a#0"
        assert event.target == "b"
        assert event.payload == {"key": "value"}

    def test_payload_defaults_none(self) -> None:
        event = DispatchEvent(source_instance="a#0", target="b")
        assert event.payload is None

    def test_frozen_immutable(self) -> None:
        event = DispatchEvent(source_instance="a#0", target="b")
        with pytest.raises(ValidationError):
            event.target = "c"  # type: ignore[misc]

    def test_extra_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            DispatchEvent(  # type: ignore[call-arg]
                source_instance="a#0",
                target="b",
                extra_field="bad",
            )


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
        result = await GraphEngine(compiled).run_async(ctx)
        assert result.count == 3

    async def test_sync_run(self) -> None:
        g: Graph[CounterState] = Graph()
        g.add_node("a", DispatchAddNode(amount=5, target=GraphNode.END))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = make_parallel_ctx(CounterState(count=0))
        result = GraphEngine(compiled).run(ctx)
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
        result = await GraphEngine(compiled).run_async(ctx)
        assert result.count == 6

    async def test_node_without_dispatch_terminates(self) -> None:
        """A node that delivers to END terminates the graph."""

        class NoDispatchNode(Node[CounterState]):
            def execute(self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput) -> NodeResult:
                ctx.state.count += 10
                self.deliver(None, None, ctx)
                return NodeResult()

        g: Graph[CounterState] = Graph()
        g.add_node("a", NoDispatchNode())
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = make_parallel_ctx(CounterState(count=0))
        result = await GraphEngine(compiled).run_async(ctx)
        assert result.count == 10

    async def test_dispatch_with_payload(self) -> None:
        """dispatch(target, state_update) records payload in DispatchEvent."""
        g: Graph[CounterState] = Graph()
        g.add_node(
            "a",
            DispatchAddWithPayloadNode(amount=1, target=GraphNode.END, payload={"data": 42}),
        )
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = make_parallel_ctx(CounterState(count=0))
        engine = GraphEngine(compiled)
        await engine.run_async(ctx)

        scheduler = engine._scheduler
        assert isinstance(scheduler, ParallelScheduler)
        assert len(scheduler._dispatch_log) == 1
        event = scheduler._dispatch_log[0]
        assert event.payload and event.payload["delivered"] == {"data": 42}
        assert event.target == GraphNode.END


# ── Single-node fast path ────────────────────────────────────────────────


class TestFastPath:
    """Single-node fast path: one READY + no RUNNING → direct main_state."""

    async def test_single_node_executes(self) -> None:
        g: Graph[CounterState] = Graph()
        g.add_node("a", DispatchAddNode(amount=7, target=GraphNode.END))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = make_parallel_ctx(CounterState(count=0))
        result = await GraphEngine(compiled).run_async(ctx)
        assert result.count == 7

    async def test_fast_path_creates_single_instance(self) -> None:
        """Fast path: only one instance created, forked_state is None."""
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
        # One instance for the entry node.
        assert len(scheduler._instances) == 1
        instance = scheduler._instances["a#0"]
        assert instance.forked_state is None
        assert instance.status == NodeInstanceStatus.COMPLETED

    async def test_fast_path_iteration_count(self) -> None:
        """Fast path increments iteration count by 1 per execution."""
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
        assert scheduler._iteration_count == 1


# ── END dispatch ──────────────────────────────────────────────────────────


class TestEndDispatch:
    async def test_dispatch_to_end_records_source(self) -> None:
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
        end_sources = {
            e.source_instance for e in scheduler._dispatch_log if e.target == GraphNode.END
        }
        assert "a#0" in end_sources
        # No instance created for END.
        end_instances = [
            inst for inst in scheduler._instances.values() if inst.node_name == GraphNode.END
        ]
        assert len(end_instances) == 0


# ── RoutingError on invalid dispatch target ──────────────────────────────


class TestRoutingError:
    async def test_dispatch_to_non_edge_target_raises(self) -> None:
        """ctx.dispatch to a target not in outgoing edges raises RoutingError."""

        class BadDispatchNode(Node[CounterState]):
            def execute(self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput) -> NodeResult:
                ctx.dispatch("nonexistent")  # no edge from "a" to "nonexistent"
                return NodeResult()

        g: Graph[CounterState] = Graph()
        g.add_node("a", BadDispatchNode())
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = make_parallel_ctx(CounterState(count=0))
        with pytest.raises(RoutingError, match="nonexistent"):
            await GraphEngine(compiled).run_async(ctx)

    async def test_dispatch_to_registered_node_without_edge_raises(self) -> None:
        """dispatch to a registered node with no edge from source raises.

        Graph: START→a, a→c, a→END, c→b, b→END. Node "a" tries to
        dispatch directly to "b" — but there's no edge a→b (only a→c
        and a→END). The intermediary "c" makes "b" reachable from
        START (satisfying PARALLEL reachability validation) without
        adding a direct a→b edge.
        """

        class DispatchToBNode(Node[CounterState]):
            def execute(self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput) -> NodeResult:
                ctx.dispatch("b")  # "b" exists but no direct edge a→b
                return NodeResult()

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
            await GraphEngine(compiled).run_async(ctx)


# ── max_iterations in parallel mode ──────────────────────────────────────


class TestMaxIterations:
    async def test_self_dispatch_loop_raises_recursion_error(self) -> None:
        """A node that dispatches to itself loops until max_iterations."""

        class SelfDispatchNode(Node[CounterState]):
            def execute(self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput) -> NodeResult:
                ctx.state.count += 1
                self.deliver(None, "loop", ctx)
                return NodeResult()

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
            await GraphEngine(compiled).run_async(ctx)
        # 5 executions before the 6th is rejected.
        assert ctx.state.count == 5

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
        await engine.run_async(ctx)

        scheduler = engine._scheduler
        assert isinstance(scheduler, ParallelScheduler)
        # Two instances executed: a#0 and b#1.
        assert scheduler._iteration_count == 2
        assert len(scheduler._instances) == 2


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
        result = await GraphEngine(compiled).run_async(ctx)
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
        calls: list[tuple[str, str, dict[str, Any] | None]] = []

        def handler(source: str, target: str, payload: dict[str, Any] | None) -> None:
            calls.append((source, target, payload))

        ctx = GraphContext(
            state=CounterState(),
            runtime=make_runtime(),
            coordinator=make_coordinator(),
            scheduler_kind=SchedulerKind.PARALLEL,
            dispatch_handler=handler,
            current_instance="a#0",
        )
        ctx.dispatch("b", {"key": "val"})
        assert calls == [("a#0", "b", {"key": "val"})]

    def test_set_current_instance(self) -> None:
        ctx = GraphContext(
            state=CounterState(),
            runtime=make_runtime(),
            coordinator=make_coordinator(),
            scheduler_kind=SchedulerKind.PARALLEL,
        )
        assert ctx._current_instance is None
        ctx.set_current_instance("llm#2")
        assert ctx._current_instance == "llm#2"

    def test_fork_inherits_dispatch_handler(self) -> None:
        def handler(s: str, t: str, p: dict[str, Any] | None) -> None:
            pass

        ctx = GraphContext(
            state=CounterState(),
            runtime=make_runtime(),
            coordinator=make_coordinator(),
            scheduler_kind=SchedulerKind.PARALLEL,
            dispatch_handler=handler,
        )
        sub = ctx.fork(state=CounterState())
        assert sub._dispatch_handler is handler

    def test_fork_does_not_inherit_current_instance(self) -> None:
        ctx = GraphContext(
            state=CounterState(),
            runtime=make_runtime(),
            coordinator=make_coordinator(),
            scheduler_kind=SchedulerKind.PARALLEL,
            current_instance="a#0",
        )
        sub = ctx.fork(state=CounterState())
        assert sub._current_instance is None


# ── Dispatch log ──────────────────────────────────────────────────────────


class TestDispatchLog:
    async def test_dispatch_log_records_all_events(self) -> None:
        g: Graph[CounterState] = Graph()
        g.add_node("a", DispatchAddNode(amount=1, target="b"))
        g.add_node("b", DispatchAddNode(amount=2, target=GraphNode.END))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", "b")
        g.add_edge("b", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = make_parallel_ctx(CounterState(count=0))
        engine = GraphEngine(compiled)
        await engine.run_async(ctx)

        scheduler = engine._scheduler
        assert isinstance(scheduler, ParallelScheduler)
        assert len(scheduler._dispatch_log) == 2
        assert scheduler._dispatch_log[0].source_instance == "a#0"
        assert scheduler._dispatch_log[0].target == "b"
        assert scheduler._dispatch_log[1].source_instance == "b#1"
        assert scheduler._dispatch_log[1].target == GraphNode.END


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

    def test_dispatch_event_in_result_module(self) -> None:
        from modex_graph.result import DispatchEvent as ResultDispatch

        assert ResultDispatch is DispatchEvent

    def test_public_exports(self) -> None:
        import modex_graph

        for name in (
            "ParallelScheduler",
            "NodeInstanceStatus",
            "NodeInstance",
            "DispatchEvent",
        ):
            assert name in modex_graph.__all__, f"{name} not in __all__"
            assert hasattr(modex_graph, name), f"{name} not importable"


# ── Routing compilation integration ─────────────────────────────────────


class TestRoutingCompilationIntegration:
    """Integration tests for deliver-based dispatch under ParallelScheduler."""

    async def test_default_edge_fires_when_no_explicit_target(self) -> None:
        """Node delivers with next_node=None → default edge fires."""

        class DeliverDefaultNode(Node[CounterState]):
            def execute(self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput) -> NodeResult:
                ctx.state.count += 1
                self.deliver(None, None, ctx)
                return NodeResult()

        g: Graph[CounterState] = Graph()
        g.add_node("a", DeliverDefaultNode())
        g.add_node("b", DispatchAddNode(amount=10, target=GraphNode.END))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", "b")
        g.add_edge("b", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = make_parallel_ctx(CounterState(count=0))
        result = await GraphEngine(compiled).run_async(ctx)
        assert result.count == 11
