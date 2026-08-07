"""End-to-end parallel scheduling tests with realistic scenarios.

These tests exercise the full ParallelScheduler pipeline — graph construction,
compile with scheduler=PARALLEL, GraphEngine.run_async, and final state
assertion — using deliver-based routing. Each scenario models a real-world
pattern that would benefit from parallel execution.

Scenarios:

1. **Parallel map-reduce**: split work → fan-out N workers in parallel →
   reduce results. Uses ``deliver()`` for fan-out and shared state for fan-in.

2. **Conditional branch with ON_ALL_PREDS join**: a router node selects
   1 or 2 branches based on state; a downstream join node waits for all
   *activated* branches. Tests the "skipped arm doesn't deadlock" property.

3. **ON_RECEIVE event aggregation**: multiple upstream nodes each produce
   events; a downstream processor fires once per event (N inputs → N
   executions), accumulating results in shared state.

4. **Mixed trigger modes in one graph**: some nodes ``ON_ALL_PREDS``,
   others ``ON_RECEIVE``, demonstrating that per-node trigger configuration
   works in a realistic mixed topology.

5. **Parallel pipeline with fan-out+join+continue**: A fans out to [B, C],
   both join at D (ON_ALL_PREDS), then D routes to E → END. Verifies
   that parallel branches converge and the graph continues past the join.
"""

from __future__ import annotations

import asyncio

from helpers import make_coordinator, make_runtime

from modex_graph import (
    Graph,
    GraphContext,
    GraphEngine,
    GraphNode,
    GraphState,
    IntegratedInput,
    Node,
    NodeTrigger,
    SchedulerKind,
)

# ── Shared state types ────────────────────────────────────────────────────


class MapReduceState(GraphState):
    """State for the parallel map-reduce scenario."""

    items: list[int] = []
    current_item: int = 0
    results: list[int] = []
    total: int = 0


class EventState(GraphState):
    """State for the event aggregation scenario."""

    current_event: str = ""
    processed: list[str] = []
    count: int = 0


class PipelineState(GraphState):
    """State for the mixed-mode pipeline scenario."""

    value: int = 0
    messages: list[str] = []
    stage: str = ""


# ── Scenario 1: Parallel map-reduce ──────────────────────────────────────


class SplitNode(Node[MapReduceState]):
    """Reads ``items`` from state, delivers to ``worker`` (triggers processing)."""

    async def execute(
        self, ctx: GraphContext[MapReduceState], integrated_input: IntegratedInput
    ) -> None:
        if ctx.state.items:
            self.deliver(None, "worker", ctx)
        else:
            self.deliver(None, "reduce", ctx)
        return None


class WorkerNode(Node[MapReduceState]):
    """Processes all items and appends their squares to shared results."""

    trigger = NodeTrigger.ON_RECEIVE

    async def execute(
        self, ctx: GraphContext[MapReduceState], integrated_input: IntegratedInput
    ) -> None:
        squares = [item * item for item in ctx.state.items]
        ctx.state.results.extend(squares)
        self.deliver(None, "reduce", ctx)
        return None


class ReduceNode(Node[MapReduceState]):
    """Reads accumulated results, computes total."""

    async def execute(
        self, ctx: GraphContext[MapReduceState], integrated_input: IntegratedInput
    ) -> None:
        results = ctx.state.results
        ctx.state.total = sum(results)
        self.deliver(None, GraphNode.END, ctx)
        return None


class TestParallelMapReduce:
    """Split → [worker×N parallel] → reduce → END.

    Workers use ``deliver()`` for fan-out and mutate shared results directly.
    The reduce node runs after all workers complete (ON_ALL_PREDS default).
    """

    def _build_graph(self) -> Graph[MapReduceState]:
        g: Graph[MapReduceState] = Graph()
        g.add_node("split", SplitNode())
        g.add_node("worker", WorkerNode())
        g.add_node("reduce", ReduceNode())
        g.add_edge(GraphNode.START, "split")
        g.add_edge("split", "worker")
        g.add_edge("split", "reduce")
        g.add_edge("worker", "reduce")
        g.add_edge("reduce", GraphNode.END)
        return g

    async def test_map_reduce_produces_correct_total(self) -> None:
        """Items [1,2,3,4] → squares [1,4,9,16] → total 30."""
        g = self._build_graph()
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = GraphContext(
            state=MapReduceState(items=[1, 2, 3, 4]),
            runtime=make_runtime(),
            coordinator=make_coordinator(),
            scheduler_kind=SchedulerKind.PARALLEL,
        )
        result = await GraphEngine(compiled).run_async(ctx)

        assert result.total == 30
        assert sorted(result.results) == [1, 4, 9, 16]

    async def test_map_reduce_single_item(self) -> None:
        """Edge case: one item → one worker → reduce."""
        g = self._build_graph()
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = GraphContext(
            state=MapReduceState(items=[7]),
            runtime=make_runtime(),
            coordinator=make_coordinator(),
            scheduler_kind=SchedulerKind.PARALLEL,
        )
        result = await GraphEngine(compiled).run_async(ctx)

        assert result.total == 49
        assert result.results == [49]

    async def test_map_reduce_empty_items(self) -> None:
        """Edge case: zero items → zero workers → reduce runs on empty list."""
        g = self._build_graph()
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = GraphContext(
            state=MapReduceState(items=[]),
            runtime=make_runtime(),
            coordinator=make_coordinator(),
            scheduler_kind=SchedulerKind.PARALLEL,
        )
        result = await GraphEngine(compiled).run_async(ctx)

        assert result.total == 0
        assert result.results == []


# ── Scenario 2: Conditional branch with ON_ALL_PREDS join ────────────────


class RouterState(GraphState):
    """State for conditional branch scenario."""

    mode: str = ""
    high_result: int = 0
    low_result: int = 0
    merged: int = 0


class RouterNode(Node[RouterState]):
    """Routes to 'high', 'low', or both based on state.mode."""

    async def execute(
        self, ctx: GraphContext[RouterState], integrated_input: IntegratedInput
    ) -> None:
        mode = ctx.state.mode
        if mode == "both":
            self.deliver(None, "high", ctx)
            self.deliver(None, "low", ctx)
        elif mode == "high":
            self.deliver(None, "high", ctx)
        else:
            self.deliver(None, "low", ctx)
        return None


class HighProcessor(Node[RouterState]):
    async def execute(
        self, ctx: GraphContext[RouterState], integrated_input: IntegratedInput
    ) -> None:
        ctx.state.high_result = 100
        self.deliver(None, "merge", ctx)
        return None


class LowProcessor(Node[RouterState]):
    async def execute(
        self, ctx: GraphContext[RouterState], integrated_input: IntegratedInput
    ) -> None:
        ctx.state.low_result = 1
        self.deliver(None, "merge", ctx)
        return None


class MergeNode(Node[RouterState]):
    """ON_ALL_PREDS: waits for all activated branches, then merges."""

    trigger = NodeTrigger.ON_ALL_PREDS

    async def execute(
        self, ctx: GraphContext[RouterState], integrated_input: IntegratedInput
    ) -> None:
        merged = ctx.state.high_result + ctx.state.low_result
        ctx.state.merged = merged
        self.deliver(None, GraphNode.END, ctx)
        return None


class TestConditionalBranchJoin:
    """Router → [high | low | both] → merge (ON_ALL_PREDS) → END.

    When mode="high", only high runs → merge fires with 1 activated source.
    When mode="both", both run → merge fires after both complete.
    """

    def _build_graph(self) -> Graph[RouterState]:
        g: Graph[RouterState] = Graph()
        g.add_node("router", RouterNode())
        g.add_node("high", HighProcessor())
        g.add_node("low", LowProcessor())
        g.add_node("merge", MergeNode())
        g.add_edge(GraphNode.START, "router")
        g.add_edge("router", "high")
        g.add_edge("router", "low")
        g.add_edge("high", "merge")
        g.add_edge("low", "merge")
        g.add_edge("merge", GraphNode.END)
        return g

    async def test_high_only_branch(self) -> None:
        """mode="high" → only high runs, merge fires with 1 source."""
        g = self._build_graph()
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = GraphContext(
            state=RouterState(mode="high"),
            runtime=make_runtime(),
            coordinator=make_coordinator(),
            scheduler_kind=SchedulerKind.PARALLEL,
        )
        result = await GraphEngine(compiled).run_async(ctx)

        assert result.high_result == 100
        assert result.low_result == 0
        assert result.merged == 100

    async def test_low_only_branch(self) -> None:
        """mode="low" → only low runs, merge fires with 1 source."""
        g = self._build_graph()
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = GraphContext(
            state=RouterState(mode="low"),
            runtime=make_runtime(),
            coordinator=make_coordinator(),
            scheduler_kind=SchedulerKind.PARALLEL,
        )
        result = await GraphEngine(compiled).run_async(ctx)

        assert result.high_result == 0
        assert result.low_result == 1
        assert result.merged == 1

    async def test_both_branches_parallel_join(self) -> None:
        """mode="both" → high+low run in parallel, merge waits for both."""
        g = self._build_graph()
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = GraphContext(
            state=RouterState(mode="both"),
            runtime=make_runtime(),
            coordinator=make_coordinator(),
            scheduler_kind=SchedulerKind.PARALLEL,
        )
        result = await GraphEngine(compiled).run_async(ctx)

        assert result.high_result == 100
        assert result.low_result == 1
        assert result.merged == 101


# ── Scenario 3: ON_RECEIVE event aggregation ─────────────────────────────


class EventSourceNode(Node[EventState]):
    """Delivers events to the processor (triggers processing of all events)."""

    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def execute(
        self, ctx: GraphContext[EventState], integrated_input: IntegratedInput
    ) -> None:
        ctx.state.current_event = ",".join(self.events)
        self.deliver(None, "processor", ctx)
        return None


class EventProcessorNode(Node[EventState]):
    """ON_RECEIVE: processes all events, appends to processed list."""

    trigger = NodeTrigger.ON_RECEIVE

    async def execute(
        self, ctx: GraphContext[EventState], integrated_input: IntegratedInput
    ) -> None:
        events = ctx.state.current_event.split(",") if ctx.state.current_event else []
        processed = [f"processed:{e}" for e in events]
        ctx.state.processed.extend(processed)
        self.deliver(None, "aggregator", ctx)
        return None


class EventAggregator(Node[EventState]):
    """Counts processed events."""

    async def execute(
        self, ctx: GraphContext[EventState], integrated_input: IntegratedInput
    ) -> None:
        ctx.state.count = len(ctx.state.processed)
        self.deliver(None, GraphNode.END, ctx)
        return None


class TestOnReceiveEventAggregation:
    """Source → [processor×N parallel, ON_RECEIVE] → aggregator → END.

    Each event triggers a separate processor instance. The aggregator
    (ON_ALL_PREDS) waits for all processors, then counts.
    """

    def _build_graph(self) -> Graph[EventState]:
        g: Graph[EventState] = Graph()
        g.add_node("source", EventSourceNode(events=["a", "b", "c"]))
        g.add_node("processor", EventProcessorNode())
        g.add_node("aggregator", EventAggregator())
        g.add_edge(GraphNode.START, "source")
        g.add_edge("source", "processor")
        g.add_edge("processor", "aggregator")
        g.add_edge("aggregator", GraphNode.END)
        return g

    async def test_three_events_three_processors(self) -> None:
        """3 events → 3 processor instances → 3 processed entries → count=3."""
        g = self._build_graph()
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = GraphContext(
            state=EventState(),
            runtime=make_runtime(),
            coordinator=make_coordinator(),
            scheduler_kind=SchedulerKind.PARALLEL,
        )
        result = await GraphEngine(compiled).run_async(ctx)

        assert result.count == 3
        assert len(result.processed) == 3
        assert "processed:a" in result.processed
        assert "processed:b" in result.processed
        assert "processed:c" in result.processed

    async def test_single_event(self) -> None:
        """1 event → 1 processor → count=1."""
        g: Graph[EventState] = Graph()
        g.add_node("source", EventSourceNode(events=["only"]))
        g.add_node("processor", EventProcessorNode())
        g.add_node("aggregator", EventAggregator())
        g.add_edge(GraphNode.START, "source")
        g.add_edge("source", "processor")
        g.add_edge("processor", "aggregator")
        g.add_edge("aggregator", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = GraphContext(
            state=EventState(),
            runtime=make_runtime(),
            coordinator=make_coordinator(),
            scheduler_kind=SchedulerKind.PARALLEL,
        )
        result = await GraphEngine(compiled).run_async(ctx)

        assert result.count == 1
        assert result.processed == ["processed:only"]


# ── Scenario 4: Mixed trigger modes ──────────────────────────────────────


class MixedFanOutNode(Node[PipelineState]):
    """Fans out to both 'left' and 'right' via deliver."""

    async def execute(
        self, ctx: GraphContext[PipelineState], integrated_input: IntegratedInput
    ) -> None:
        ctx.state.value = 1
        self.deliver(None, "left", ctx)
        self.deliver(None, "right", ctx)
        return None


class LeftNode(Node[PipelineState]):
    """ON_RECEIVE: processes each incoming dispatch independently."""

    trigger = NodeTrigger.ON_RECEIVE

    async def execute(
        self, ctx: GraphContext[PipelineState], integrated_input: IntegratedInput
    ) -> None:
        ctx.state.messages.append("left")
        self.deliver(None, "join", ctx)
        return None


class RightNode(Node[PipelineState]):
    """ON_ALL_PREDS (default): waits for all activated sources."""

    async def execute(
        self, ctx: GraphContext[PipelineState], integrated_input: IntegratedInput
    ) -> None:
        ctx.state.messages.append("right")
        self.deliver(None, "join", ctx)
        return None


class JoinNode(Node[PipelineState]):
    """ON_ALL_PREDS: waits for both left and right before executing."""

    trigger = NodeTrigger.ON_ALL_PREDS

    async def execute(
        self, ctx: GraphContext[PipelineState], integrated_input: IntegratedInput
    ) -> None:
        ctx.state.stage = "joined"
        self.deliver(None, "continue", ctx)
        return None


class ContinueNode(Node[PipelineState]):
    async def execute(
        self, ctx: GraphContext[PipelineState], integrated_input: IntegratedInput
    ) -> None:
        ctx.state.value += 10
        self.deliver(None, GraphNode.END, ctx)
        return None


class TestMixedTriggerModes:
    """FanOut → [Left (ON_RECEIVE), Right (ON_ALL_PREDS)] → Join (ON_ALL_PREDS) → Continue → END.

    Left and Right have different trigger modes but both dispatch to Join.
    Join (ON_ALL_PREDS) waits for both. Graph continues past the join.
    """

    def _build_graph(self) -> Graph[PipelineState]:
        g: Graph[PipelineState] = Graph()
        g.add_node("fan_out", MixedFanOutNode())
        g.add_node("left", LeftNode())
        g.add_node("right", RightNode())
        g.add_node("join", JoinNode())
        g.add_node("continue", ContinueNode())
        g.add_edge(GraphNode.START, "fan_out")
        g.add_edge("fan_out", "left")
        g.add_edge("fan_out", "right")
        g.add_edge("left", "join")
        g.add_edge("right", "join")
        g.add_edge("join", "continue")
        g.add_edge("continue", GraphNode.END)
        return g

    async def test_mixed_modes_join_and_continue(self) -> None:
        """Both branches fire, join waits, graph continues to 'continue' node."""
        g = self._build_graph()
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = GraphContext(
            state=PipelineState(),
            runtime=make_runtime(),
            coordinator=make_coordinator(),
            scheduler_kind=SchedulerKind.PARALLEL,
        )
        result = await GraphEngine(compiled).run_async(ctx)

        assert result.value == 11
        assert result.stage == "joined"
        assert "left" in result.messages
        assert "right" in result.messages
        assert len(result.messages) == 2


# ── Scenario 5: Parallel pipeline with async nodes ───────────────────────


class AsyncWorkState(GraphState):
    """State for async parallel pipeline."""

    work_val: int = 0
    results: list[int] = []
    final: int = 0


class AsyncSplitNode(Node[AsyncWorkState]):
    """Delivers to 'worker' (triggers async processing)."""

    async def execute(
        self, ctx: GraphContext[AsyncWorkState], integrated_input: IntegratedInput
    ) -> None:
        self.deliver(None, "worker", ctx)
        return None


class AsyncWorkerNode(Node[AsyncWorkState]):
    """Async node that simulates I/O work, then writes result."""

    trigger = NodeTrigger.ON_RECEIVE

    async def execute(
        self, ctx: GraphContext[AsyncWorkState], integrated_input: IntegratedInput
    ) -> None:
        await asyncio.sleep(0.01)
        val = ctx.state.work_val
        results = [val * 2 + 1, val * 3 + 1]
        ctx.state.results.extend(results)
        self.deliver(None, "collect", ctx)
        return None


class AsyncCollectNode(Node[AsyncWorkState]):
    """Waits for all workers (ON_ALL_PREDS), sums results."""

    async def execute(
        self, ctx: GraphContext[AsyncWorkState], integrated_input: IntegratedInput
    ) -> None:
        await asyncio.sleep(0.01)
        ctx.state.final = sum(ctx.state.results)
        self.deliver(None, GraphNode.END, ctx)
        return None


class TestAsyncParallelPipeline:
    """Split → [async worker×2 parallel] → collect (async, ON_ALL_PREDS) → END.

    Workers are truly async (``asyncio.sleep``), exercising the
    ``asyncio.gather`` concurrent execution path.
    """

    def _build_graph(self) -> Graph[AsyncWorkState]:
        g: Graph[AsyncWorkState] = Graph()
        g.add_node("split", AsyncSplitNode())
        g.add_node("worker", AsyncWorkerNode())
        g.add_node("collect", AsyncCollectNode())
        g.add_edge(GraphNode.START, "split")
        g.add_edge("split", "worker")
        g.add_edge("worker", "collect")
        g.add_edge("collect", GraphNode.END)
        return g

    async def test_async_workers_run_concurrently(self) -> None:
        """work_val=5 → workers get 10 and 15 → results [11, 16] → final 27."""
        g = self._build_graph()
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = GraphContext(
            state=AsyncWorkState(work_val=5),
            runtime=make_runtime(),
            coordinator=make_coordinator(),
            scheduler_kind=SchedulerKind.PARALLEL,
        )
        result = await GraphEngine(compiled).run_async(ctx)

        assert result.final == 27
        assert sorted(result.results) == [11, 16]

    async def test_async_concurrent_timing(self) -> None:
        """Two workers each sleep 50ms. If sequential, total ≥ 100ms.
        If parallel, total ≈ 50ms. We assert < 90ms to allow overhead."""
        g: Graph[AsyncWorkState] = Graph()
        g.add_node("split", AsyncSplitNode())
        g.add_node("worker", AsyncWorkerNode())
        g.add_node("collect", AsyncCollectNode())
        g.add_edge(GraphNode.START, "split")
        g.add_edge("split", "worker")
        g.add_edge("worker", "collect")
        g.add_edge("collect", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = GraphContext(
            state=AsyncWorkState(work_val=1),
            runtime=make_runtime(),
            coordinator=make_coordinator(),
            scheduler_kind=SchedulerKind.PARALLEL,
        )

        import time

        start = time.monotonic()
        await GraphEngine(compiled).run_async(ctx)
        elapsed = time.monotonic() - start

        # Two workers each sleep 10ms + collect sleeps 10ms.
        # Sequential would be ~30ms. Parallel workers → ~20ms.
        # Allow generous margin for CI overhead.
        assert elapsed < 0.5, f"Elapsed {elapsed:.3f}s suggests sequential execution"
