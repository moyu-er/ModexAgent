"""Invariant tests for the graph runtime convergence (suggestion §20-24).

Covers:
- Test D: unrelated straggler doesn't block dependent nodes.
- Test E: same source multiple delivers batch into one ON_ALL_PREDS
  invocation.
- Test F: PARALLEL MapReduce E2E via pure deliver dataflow.
- §23: invocation identity concurrency — concurrent tasks don't clobber
  each other's source identity in delivers. Verified via
  IntegratedPayload.source_node, not content values.
- §24: ctx.scratch ownership — two nodes writing to ctx.scratch don't
  overwrite each other.
- ON_ALL_PREDS serial gate overlap: second group arrives while first
  invocation is suspended — must not fire until first completes.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from modex_graph import (
    DefaultGraphState,
    Graph,
    GraphContext,
    GraphEngine,
    GraphNode,
    GraphState,
    IntegratedInput,
    Node,
    SchedulerKind,
)
from modex_graph.scheduler.bootstrap import BootstrapMode
from tests.unit.modex_graph.helpers import make_coordinator, register_graph_nodes


class CounterState(GraphState):
    count: int = 0
    messages: list[str] = []


class NoOpRuntime:
    async def before_node(self, ctx: Any, node_name: str) -> None:
        pass

    async def after_node(self, ctx: Any, node_name: str) -> None:
        pass


def make_parallel_ctx(state: GraphState, compiled: Any) -> GraphContext[Any]:
    coord = make_coordinator()
    register_graph_nodes(coord, compiled)
    return GraphContext(
        state=state,
        runtime=NoOpRuntime(),
        coordinator=coord,
        scheduler_kind=SchedulerKind.PARALLEL,
    )


class FanOutStartNode(Node[CounterState]):
    def __init__(self, targets: list[str]) -> None:
        self.targets = targets

    async def execute(self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput) -> None:
        for t in self.targets:
            self.deliver(None, t, ctx)


class DelayDispatchNode(Node[CounterState]):
    def __init__(self, target: str, delay: float = 0.0, label: str = "") -> None:
        self.target = target
        self.delay = delay
        self.label = label or target

    async def execute(self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput) -> None:
        if self.delay:
            await asyncio.sleep(self.delay)
        ctx.state.messages.append(f"done:{self.label}")
        self.deliver(None, self.target, ctx)


class RecordDispatchNode(Node[CounterState]):
    def __init__(self, target: str | None = None) -> None:
        self.target = target

    async def execute(self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput) -> None:
        label = ctx._current_instance or self.name
        ctx.state.messages.append(f"exec:{self.name}:{label}")
        if self.target is not None:
            self.deliver(None, self.target, ctx)


class TestUnrelatedStraggler:
    async def test_unrelated_slow_node_does_not_block_b(self) -> None:
        g: Graph[CounterState] = Graph()
        g.add_node("fanout", FanOutStartNode(targets=["a", "c"]))
        g.add_node("a", DelayDispatchNode(target="b", delay=0.0, label="a"))
        g.add_node("c", DelayDispatchNode(target=GraphNode.END, delay=0.2, label="c"))
        g.add_node("b", RecordDispatchNode(target=GraphNode.END))
        g.add_edge(GraphNode.START, "fanout")
        g.add_edge("fanout", "a")
        g.add_edge("fanout", "c")
        g.add_edge("a", "b")
        g.add_edge("b", GraphNode.END)
        g.add_edge("c", GraphNode.END)
        compiled = g.compile(max_iterations=20, scheduler=SchedulerKind.PARALLEL)
        ctx = make_parallel_ctx(CounterState(), compiled)
        engine = GraphEngine(compiled)
        await engine.run_async(ctx, mode=BootstrapMode.FRESH)

        done_a = ctx.state.messages.index("done:a")
        done_b = next(i for i, m in enumerate(ctx.state.messages) if m.startswith("exec:b:"))
        done_c = ctx.state.messages.index("done:c")
        assert done_a < done_b < done_c


class TestSameSourceMultipleDelivers:
    async def test_three_delivers_one_invocation(self) -> None:
        class FanOutNode(Node[CounterState]):
            def __init__(self, items: list[Any], target: str) -> None:
                self.items = items
                self.target = target

            async def execute(self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput) -> None:
                for item in self.items:
                    self.deliver(item, self.target, ctx)

        class CollectNode(Node[CounterState]):
            async def execute(self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput) -> None:
                values = [p.content for p in integrated_input.payloads]
                ctx.state.messages.append(f"collected:{len(values)}")
                self.deliver(None, GraphNode.END, ctx)

        g: Graph[CounterState] = Graph()
        g.add_node("a", FanOutNode(items=[1, 2, 3], target="b"))
        g.add_node("b", CollectNode())
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", "b")
        g.add_edge("b", GraphNode.END)
        compiled = g.compile(max_iterations=10, scheduler=SchedulerKind.PARALLEL)
        ctx = make_parallel_ctx(CounterState(), compiled)
        engine = GraphEngine(compiled)
        await engine.run_async(ctx, mode=BootstrapMode.FRESH)

        assert "collected:3" in ctx.state.messages


class TestParallelMapReduceE2E:
    async def test_map_reduce_parallel(self) -> None:
        import sys
        from pathlib import Path

        examples_root = Path(__file__).resolve().parents[3] / "examples"
        if str(examples_root) not in sys.path:
            sys.path.insert(0, str(examples_root))
        from graph_patterns.map_reduce import MapNode, ReduceNode

        class WorkerNode(Node[DefaultGraphState]):
            async def execute(self, ctx: GraphContext[DefaultGraphState], integrated_input: IntegratedInput) -> None:
                for payload in integrated_input.payloads:
                    self.deliver(payload.content * payload.content, "reduce", ctx)

        class ItemState(DefaultGraphState):
            items: list[int] = [1, 2, 3]

        g: Graph[DefaultGraphState] = Graph()
        g.add_node("map", MapNode(items_fn=lambda s: s.items, worker_node="worker"))
        g.add_node("worker", WorkerNode())
        g.add_node("reduce", ReduceNode(reducer=sum))
        g.add_edge(GraphNode.START, "map")
        g.add_edge("map", "worker")
        g.add_edge("worker", "reduce")
        g.add_edge("reduce", GraphNode.END)
        compiled = g.compile(max_iterations=10, scheduler=SchedulerKind.PARALLEL)

        coord = make_coordinator()
        register_graph_nodes(coord, compiled)
        ctx: GraphContext[DefaultGraphState] = GraphContext(
            state=ItemState(),
            runtime=NoOpRuntime(),
            coordinator=coord,
            scheduler_kind=SchedulerKind.PARALLEL,
        )
        engine = GraphEngine(compiled)
        await engine.run_async(ctx, mode=BootstrapMode.FRESH)

        assert len(ctx.state.result) == 1
        assert int(ctx.state.result[0].content) == 14


class TestInvocationIdentityConcurrency:
    """Concurrent tasks must not clobber each other's invocation identity.
    Verified via IntegratedPayload.source_node (framework-set field),
    not via content values that could be the same regardless of source."""

    async def test_concurrent_delivers_have_correct_source(self) -> None:
        class SourceNode(Node[CounterState]):
            def __init__(self, target: str, event: asyncio.Event) -> None:
                self.target = target
                self.event = event

            async def execute(self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput) -> None:
                await self.event.wait()
                self.deliver(self.name, self.target, ctx)

        class SinkNode(Node[CounterState]):
            async def execute(self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput) -> None:
                source_nodes = sorted(p.source_node for p in integrated_input.payloads)
                ctx.state.messages.append(f"sink:{source_nodes}")
                self.deliver(None, GraphNode.END, ctx)

        event = asyncio.Event()
        g: Graph[CounterState] = Graph()
        g.add_node("fanout", FanOutStartNode(targets=["a", "b"]))
        g.add_node("a", SourceNode(target="d", event=event))
        g.add_node("b", SourceNode(target="d", event=event))
        g.add_node("d", SinkNode())
        g.add_edge(GraphNode.START, "fanout")
        g.add_edge("fanout", "a")
        g.add_edge("fanout", "b")
        g.add_edge("a", "d")
        g.add_edge("b", "d")
        g.add_edge("d", GraphNode.END)
        compiled = g.compile(max_iterations=10, scheduler=SchedulerKind.PARALLEL)

        coord = make_coordinator()
        register_graph_nodes(coord, compiled)
        ctx: GraphContext[CounterState] = GraphContext(
            state=CounterState(),
            runtime=NoOpRuntime(),
            coordinator=coord,
            scheduler_kind=SchedulerKind.PARALLEL,
        )

        engine = GraphEngine(compiled)
        run_task = asyncio.create_task(engine.run_async(ctx, mode=BootstrapMode.FRESH))
        await asyncio.sleep(0.05)
        event.set()
        await run_task

        sink_msgs = [m for m in ctx.state.messages if m.startswith("sink:")]
        assert len(sink_msgs) == 1
        a_node_id = compiled.nodes["a"].node_id
        b_node_id = compiled.nodes["b"].node_id
        assert a_node_id in sink_msgs[0]
        assert b_node_id in sink_msgs[0]
        assert sink_msgs[0].count(a_node_id) == 1
        assert sink_msgs[0].count(b_node_id) == 1


class TestOnAllPredsSerialGateOverlap:
    """ON_ALL_PREDS serial gate: a second group must not fire while the
    first invocation of the same node is still RUNNING."""

    async def test_second_group_waits_for_first(self) -> None:
        first_started = asyncio.Event()
        first_release = asyncio.Event()

        class StartFanOut(Node[CounterState]):
            """Delivers to both group-1 and group-2 sources."""
            async def execute(self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput) -> None:
                self.deliver("g1", "src1a", ctx)
                self.deliver("g1", "src1b", ctx)
                self.deliver("g2", "src2a", ctx)
                self.deliver("g2", "src2b", ctx)

        class PassThroughNode(Node[CounterState]):
            async def execute(self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput) -> None:
                for p in integrated_input.payloads:
                    self.deliver(p.content, "sink", ctx)

        class SerialGateSink(Node[CounterState]):
            async def execute(self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput) -> None:
                labels = [p.content for p in integrated_input.payloads]
                if "g1" in labels:
                    first_started.set()
                    await first_release.wait()
                ctx.state.messages.append(f"exec:{labels}")
                self.deliver(None, GraphNode.END, ctx)

        g: Graph[CounterState] = Graph()
        g.add_node("start", StartFanOut())
        g.add_node("src1a", PassThroughNode())
        g.add_node("src1b", PassThroughNode())
        g.add_node("src2a", PassThroughNode())
        g.add_node("src2b", PassThroughNode())
        g.add_node("sink", SerialGateSink())
        g.add_edge(GraphNode.START, "start")
        g.add_edge("start", "src1a")
        g.add_edge("start", "src1b")
        g.add_edge("start", "src2a")
        g.add_edge("start", "src2b")
        g.add_edge("src1a", "sink")
        g.add_edge("src1b", "sink")
        g.add_edge("src2a", "sink")
        g.add_edge("src2b", "sink")
        g.add_edge("sink", GraphNode.END)
        compiled = g.compile(max_iterations=20, scheduler=SchedulerKind.PARALLEL)

        coord = make_coordinator()
        register_graph_nodes(coord, compiled)
        ctx: GraphContext[CounterState] = GraphContext(
            state=CounterState(),
            runtime=NoOpRuntime(),
            coordinator=coord,
            scheduler_kind=SchedulerKind.PARALLEL,
        )

        engine = GraphEngine(compiled)
        run_task = asyncio.create_task(engine.run_async(ctx, mode=BootstrapMode.FRESH))
        await asyncio.wait_for(first_started.wait(), timeout=2.0)
        messages_during_first = list(ctx.state.messages)
        first_release.set()
        await run_task

        assert len(messages_during_first) == 0
        assert len(ctx.state.messages) == 1
        all_labels = ctx.state.messages[0]
        assert "g1" in all_labels
        assert "g2" in all_labels


class TestScratchOwnership:
    async def test_two_nodes_scratch_isolation(self) -> None:
        class ScratchWriter(Node[CounterState]):
            def __init__(self, key: str, value: str, target: str) -> None:
                self.key = key
                self.value = value
                self.target = target

            async def execute(self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput) -> None:
                ctx.scratch[self.key] = self.value
                self.deliver(None, self.target, ctx)

        class ScratchReader(Node[CounterState]):
            def __init__(self, expected_key: str) -> None:
                self.expected_key = expected_key

            async def execute(self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput) -> None:
                val = ctx.scratch.get(self.expected_key)
                ctx.state.messages.append(f"read:{self.expected_key}={val}")
                self.deliver(None, GraphNode.END, ctx)

        g: Graph[CounterState] = Graph()
        g.add_node("fanout", FanOutStartNode(targets=["a", "b"]))
        g.add_node("a", ScratchWriter(key="a_key", value="a_val", target="c"))
        g.add_node("b", ScratchWriter(key="b_key", value="b_val", target="c"))
        g.add_node("c", ScratchReader("c_key"))
        g.add_edge(GraphNode.START, "fanout")
        g.add_edge("fanout", "a")
        g.add_edge("fanout", "b")
        g.add_edge("a", "c")
        g.add_edge("b", "c")
        g.add_edge("c", GraphNode.END)
        compiled = g.compile(max_iterations=10, scheduler=SchedulerKind.PARALLEL)
        ctx = make_parallel_ctx(CounterState(), compiled)
        engine = GraphEngine(compiled)
        await engine.run_async(ctx, mode=BootstrapMode.FRESH)

        a_id = compiled.nodes["a"].node_id
        b_id = compiled.nodes["b"].node_id
        assert ctx.state.node_scratch[a_id]["a_key"] == "a_val"
        assert ctx.state.node_scratch[b_id]["b_key"] == "b_val"
        read_msgs = [m for m in ctx.state.messages if m.startswith("read:")]
        assert len(read_msgs) == 1
        assert "c_key=None" in read_msgs[0]

    async def test_scratch_raises_outside_execution(self) -> None:
        coord = make_coordinator()
        ctx: GraphContext[CounterState] = GraphContext(
            state=CounterState(),
            runtime=NoOpRuntime(),
            coordinator=coord,
            scheduler_kind=SchedulerKind.PARALLEL,
        )
        with pytest.raises(RuntimeError, match="no active invocation"):
            _ = ctx.scratch
