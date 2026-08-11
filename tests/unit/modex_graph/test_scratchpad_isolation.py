"""Scratchpad isolation guard tests — verify parallel nodes only write to their own node_scratch key.

These tests verify that ``node_scratch`` provides key-based isolation under
``ParallelScheduler``. The model is simple: each node writes to
``node_scratch[<own_key>]``, and key separation provides isolation without
context forking or deepcopy.

Tests:

1. **Parallel state write isolation**: diamond graph (START -> fan_out -> A/B -> END).
   Node A writes ``node_scratch["A"]``, Node B writes ``node_scratch["B"]``.
   After completion, both keys exist with correct values -- no cross-contamination.

2. **Checkpoint includes scratch**: after running a graph, verify
   ``ctx.state.checkpoint()`` includes ``node_scratch`` with all keys.

3. **Fan-out scratch**: Map delivers 3 items to Worker (ON_RECEIVE).
   Each Worker writes ``node_scratch["worker"] = <item>``. After completion,
   worker scratch has the last item (serial gate means sequential execution --
   each overwrites the same key). This documents that ON_RECEIVE serial
   execution means same-key writes are sequential, not parallel.

4. **No cross-node write (convention boundary)**: Node A writes ``node_scratch["B"]``.
   The dict is open -- this is possible but discouraged by convention.
   The guard is convention + code review, not runtime enforcement.
"""

from __future__ import annotations

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

# -- State -----------------------------------------------------------------


class ScratchState(GraphState):
    """State for scratchpad isolation tests -- inherits node_scratch from GraphState."""

    items: list[str] = []


# -- Test 1 + 2: Diamond graph with parallel A/B nodes ----------------------


class FanOutNode(Node[ScratchState]):
    """Delivers to both 'A' and 'B' -- triggers parallel execution."""

    async def execute(
        self, ctx: GraphContext[ScratchState], integrated_input: IntegratedInput
    ) -> None:
        self.deliver(None, "A", ctx)
        self.deliver(None, "B", ctx)
        return None


class NodeA(Node[ScratchState]):
    """Writes node_scratch['A'] = 'value_a', delivers to END."""

    async def execute(
        self, ctx: GraphContext[ScratchState], integrated_input: IntegratedInput
    ) -> None:
        ctx.state.node_scratch["A"] = "value_a"
        self.deliver(None, GraphNode.END, ctx)
        return None


class NodeB(Node[ScratchState]):
    """Writes node_scratch['B'] = 'value_b', delivers to END."""

    async def execute(
        self, ctx: GraphContext[ScratchState], integrated_input: IntegratedInput
    ) -> None:
        ctx.state.node_scratch["B"] = "value_b"
        self.deliver(None, GraphNode.END, ctx)
        return None


class TestScratchpadIsolation:
    """Verify node_scratch key isolation under ParallelScheduler."""

    def _build_diamond_graph(self) -> Graph[ScratchState]:
        """Diamond: START -> fan_out -> {A, B} -> END (ON_ALL_PREDS)."""
        g: Graph[ScratchState] = Graph()
        g.add_node("fan_out", FanOutNode())
        g.add_node("A", NodeA())
        g.add_node("B", NodeB())
        g.add_edge(GraphNode.START, "fan_out")
        g.add_edge("fan_out", "A")
        g.add_edge("fan_out", "B")
        g.add_edge("A", GraphNode.END)
        g.add_edge("B", GraphNode.END)
        return g

    async def test_parallel_state_write_isolation(self) -> None:
        """Diamond: A writes scratch['A'], B writes scratch['B'] -- both survive, no cross-contamination."""
        g = self._build_diamond_graph()
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = GraphContext(
            state=ScratchState(),
            runtime=make_runtime(),
            coordinator=make_coordinator(),
            scheduler_kind=SchedulerKind.PARALLEL,
        )
        result = await GraphEngine(compiled).run_async(ctx)

        assert result.node_scratch["A"] == "value_a"
        assert result.node_scratch["B"] == "value_b"

    async def test_checkpoint_includes_scratch(self) -> None:
        """After running, checkpoint() includes node_scratch with all keys."""
        g = self._build_diamond_graph()
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = GraphContext(
            state=ScratchState(),
            runtime=make_runtime(),
            coordinator=make_coordinator(),
            scheduler_kind=SchedulerKind.PARALLEL,
        )
        result = await GraphEngine(compiled).run_async(ctx)

        checkpoint = result.checkpoint()
        assert "node_scratch" in checkpoint
        assert checkpoint["node_scratch"]["A"] == "value_a"
        assert checkpoint["node_scratch"]["B"] == "value_b"


# -- Test 3: Fan-out scratch with ON_RECEIVE worker -------------------------


class MapNode(Node[ScratchState]):
    """Delivers each item to 'worker' -- triggers N worker executions."""

    async def execute(
        self, ctx: GraphContext[ScratchState], integrated_input: IntegratedInput
    ) -> None:
        for item in ctx.state.items:
            self.deliver(item, "worker", ctx)
        return None


class WorkerNode(Node[ScratchState]):
    """ON_RECEIVE: writes node_scratch['worker'] = <delivered item>, delivers to END.

    The deliver store batches all pending delivers to the first worker instance.
    The worker iterates over all received payloads, writing each to the same key --
    the last write wins. Subsequent queued instances (from the ON_RECEIVE serial
    gate) receive no payloads and write nothing. This documents that ON_RECEIVE
    serial execution means same-key writes are sequential, not parallel.
    """

    trigger = NodeTrigger.ON_RECEIVE

    async def execute(
        self, ctx: GraphContext[ScratchState], integrated_input: IntegratedInput
    ) -> None:
        for payload in integrated_input.payloads:
            ctx.state.node_scratch["worker"] = payload.content
        self.deliver(None, GraphNode.END, ctx)
        return None


class TestFanOutScratch:
    """Map delivers 3 items -> Worker fires (ON_RECEIVE serial gate).

    Each worker execution writes to the same key ``node_scratch['worker']``.
    Serial execution means each overwrites the previous -- the final value is
    the last item. This documents that ON_RECEIVE serial execution means
    same-key writes are sequential, not parallel.
    """

    def _build_graph(self) -> Graph[ScratchState]:
        g: Graph[ScratchState] = Graph()
        g.add_node("map", MapNode())
        g.add_node("worker", WorkerNode())
        g.add_edge(GraphNode.START, "map")
        g.add_edge("map", "worker")
        g.add_edge("worker", GraphNode.END)
        return g

    async def test_fan_out_scratch_last_item_wins(self) -> None:
        """3 items -> worker writes scratch['worker'] for each -> final value is 'z'."""
        g = self._build_graph()
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = GraphContext(
            state=ScratchState(items=["x", "y", "z"]),
            runtime=make_runtime(),
            coordinator=make_coordinator(),
            scheduler_kind=SchedulerKind.PARALLEL,
        )
        result = await GraphEngine(compiled).run_async(ctx)

        assert result.node_scratch["worker"] == "z"


# -- Test 4: No cross-node write (convention boundary) ----------------------


class HackNodeA(Node[ScratchState]):
    """Writes both node_scratch['A'] (own key) and node_scratch['B'] (cross-node).

    The dict is open -- this is possible but discouraged by convention.
    The guard is convention + code review, not runtime enforcement.
    """

    async def execute(
        self, ctx: GraphContext[ScratchState], integrated_input: IntegratedInput
    ) -> None:
        ctx.state.node_scratch["A"] = "own_value"
        ctx.state.node_scratch["B"] = "hacked"
        self.deliver(None, GraphNode.END, ctx)
        return None


class TestNoCrossNodeWrite:
    """Document the convention boundary: node_scratch is open, isolation is convention-based."""

    async def test_cross_node_write_is_possible_but_discouraged(self) -> None:
        """Node A writes node_scratch['B'] -- the dict is open, no runtime enforcement.

        This test documents that the guard is convention + code review, not
        a runtime check. The dict is a plain ``dict[str, Any]`` -- any node can
        write to any key. The isolation contract is: "each node writes only
        to ``node_scratch[<own_key>]``", enforced by code review, not the runtime.
        """
        g: Graph[ScratchState] = Graph()
        g.add_node("A", HackNodeA())
        g.add_edge(GraphNode.START, "A")
        g.add_edge("A", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = GraphContext(
            state=ScratchState(),
            runtime=make_runtime(),
            coordinator=make_coordinator(),
            scheduler_kind=SchedulerKind.PARALLEL,
        )
        result = await GraphEngine(compiled).run_async(ctx)

        # A's own key -- written correctly
        assert result.node_scratch["A"] == "own_value"
        # B's key -- A was able to write to it (dict is open, no enforcement)
        assert result.node_scratch["B"] == "hacked"
