# ruff: noqa: ANN401
from __future__ import annotations

from typing import Any

import pytest
from helpers import CounterState, make_coordinator, make_ctx, make_runtime

from modex_graph import (
    DefaultInputIntegrator,
    DeliverConsumptionStatus,
    GraphContext,
    GraphNode,
    InputIntegrator,
    IntegratedInput,
    IntegratedPayload,
    Node,
    RoutingError,
    SchedulerKind,
)
from modex_graph.scheduler.bootstrap import BootstrapMode

# ── Test node subclasses ──────────────────────────────────────────────────


class _NoDeliverNode(Node[CounterState]):
    async def execute(
        self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput
    ) -> None:
        ctx.state.count += 1
        return None


class _SingleDeliverNode(Node[CounterState]):
    """Node that delivers once to an explicit next_node."""

    async def execute(
        self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput
    ) -> None:
        self.deliver("payload_a", "downstream_a", ctx)
        return None


class _MultiDeliverNode(Node[CounterState]):
    """Node that delivers multiple times to different next_nodes."""

    async def execute(
        self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput
    ) -> None:
        self.deliver("data_1", "target_x", ctx)
        self.deliver("data_2", "target_x", ctx)
        self.deliver("data_3", "target_y", ctx)
        return None


class _AsyncDeliverNode(Node[CounterState]):
    """Async node that delivers during async execute."""

    async def execute(
        self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput
    ) -> None:
        self.deliver("async_data", "async_target", ctx)
        return None


class _DeliverWithCtxNode(Node[CounterState]):
    """Node that passes ctx explicitly to deliver()."""

    async def execute(
        self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput
    ) -> None:
        self.deliver("explicit_ctx_data", "explicit_target", ctx)
        return None


class _ReadIntegratedInputNode(Node[CounterState]):
    """Node that reads integrated_input during execute."""

    async def execute(
        self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput
    ) -> None:
        if integrated_input.integrated_content is not None:
            ctx.state.name = str(integrated_input.integrated_content)
        self.deliver("read", "read_target", ctx)
        return None


class _CustomDeliverNode(Node[CounterState]):
    """Node that overrides deliver() to add a prefix."""

    def deliver(
        self,
        content: Any,
        next_node: str | None,
        ctx: GraphContext[CounterState],
    ) -> None:
        super().deliver(f"custom:{content}", next_node, ctx)

    async def execute(
        self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput
    ) -> None:
        self.deliver("data", "target", ctx)
        return None


# ── Custom InputIntegrator for testing ────────────────────────────────────


class _JoinIntegrator(InputIntegrator):
    """Joins string contents with a separator."""

    def integrate(self, payloads: list[IntegratedPayload]) -> IntegratedInput:
        joined = " + ".join(str(p.content) for p in payloads) if payloads else ""
        return IntegratedInput(payloads=payloads, integrated_content=joined)


# ── Parallel ctx helper ───────────────────────────────────────────────────


def _make_parallel_ctx(
    state: CounterState | None = None,
) -> tuple[GraphContext[CounterState], list[tuple[str, str]]]:
    """Build a PARALLEL ctx with a recording dispatch handler.

    Returns (ctx, dispatch_calls) where dispatch_calls records each pure-wakeup
    (source_instance, target) pair passed to the handler.
    """
    state = state if state is not None else CounterState()
    ctx: GraphContext[CounterState] = GraphContext(
        state=state,
        runtime=make_runtime(),
        coordinator=make_coordinator(),
        scheduler_kind=SchedulerKind.PARALLEL,
    )
    dispatch_calls: list[tuple[str, str]] = []

    def handler(
        source_instance: str,
        target: str,
    ) -> None:
        dispatch_calls.append((source_instance, target))

    ctx.set_dispatch_handler(handler)
    return ctx, dispatch_calls


def _make_linear_ctx(state: CounterState | None = None) -> GraphContext[CounterState]:
    return make_ctx(state)


def _target_contents(ctx: GraphContext[CounterState], target: str) -> list[Any]:
    store = ctx.coordinator.get_deliver_store(target)
    assert store is not None
    return [
        record.content
        for record in store.query_consumable(ctx.coordinator.graph_instance_id, target)
    ]


# ── Node attributes ───────────────────────────────────────────────────────


class TestNodeAttributes:
    def test_input_integrator_defaults_to_default(self) -> None:
        node = _NoDeliverNode()
        assert isinstance(node.input_integrator, DefaultInputIntegrator)

    def test_input_integrator_can_be_overridden(self) -> None:
        node = _NoDeliverNode()
        custom = _JoinIntegrator()
        node.input_integrator = custom
        assert node.input_integrator is custom


# ── run: basic orchestration ──────────────────────────────────────────────


class TestExecuteBasic:
    async def test_execute_calls_execute_and_returns_none(self) -> None:
        node = _SingleDeliverNode()
        node.name = "single_deliver"
        ctx = _make_linear_ctx(CounterState(count=0))
        result = await node.run(ctx)
        assert result is None
        assert _target_contents(ctx, "downstream_a") == ["payload_a"]

    async def test_each_execution_persists_its_deliver(self) -> None:
        node = _SingleDeliverNode()
        node.name = "single_deliver"
        ctx = _make_linear_ctx()
        await node.run(ctx)
        await node.run(ctx)
        assert _target_contents(ctx, "downstream_a") == ["payload_a", "payload_a"]

    async def test_execute_with_upstream_payloads(self) -> None:
        node = _ReadIntegratedInputNode()
        node.name = "read_integrated"
        node.node_id = "node-read-integrated"
        ctx = _make_linear_ctx(CounterState(name=""))
        ctx.coordinator.register_node(node.node_id)
        store = ctx.coordinator.get_deliver_store(node.node_id)
        assert store is not None
        store.accumulate(
            graph_instance_id=0,
            node_id=node.node_id,
            source_node_id="up_a",
            source_invocation_id=1,
            content="hello",
        )
        store.accumulate(
            graph_instance_id=0,
            node_id=node.node_id,
            source_node_id="up_b",
            source_invocation_id=2,
            content="world",
        )
        await node.run(ctx)
        assert ctx.state.name == "['hello', 'world']"

    async def test_execute_with_no_upstream_payloads(self) -> None:
        node = _ReadIntegratedInputNode()
        node.name = "read_integrated"
        node.node_id = "node-read-integrated"
        ctx = _make_linear_ctx(CounterState(name="initial"))
        await node.run(ctx)
        # Default integrator on empty list -> integrated_content = []
        assert ctx.state.name == "[]"

    async def test_execute_with_custom_integrator(self) -> None:
        node = _ReadIntegratedInputNode()
        node.name = "read_integrated"
        node.input_integrator = _JoinIntegrator()
        ctx = _make_linear_ctx(CounterState(name=""))
        ctx.coordinator.register_node(node.node_id)
        store = ctx.coordinator.get_deliver_store(node.node_id)
        assert store is not None
        store.accumulate(
            graph_instance_id=0,
            node_id=node.node_id,
            source_node_id="up_a",
            source_invocation_id=1,
            content="hello",
        )
        store.accumulate(
            graph_instance_id=0,
            node_id=node.node_id,
            source_node_id="up_b",
            source_invocation_id=2,
            content="world",
        )
        await node.run(ctx)
        assert ctx.state.name == "hello + world"

    async def test_execute_with_async_node_returns_none(self) -> None:
        node = _AsyncDeliverNode()
        node.name = "async_deliver"
        ctx = _make_linear_ctx()
        result = await node.run(ctx)
        assert result is None
        assert _target_contents(ctx, "async_target") == ["async_data"]


# ── deliver: target-side persistence ──────────────────────────────────────


class TestDeliverPersistence:
    async def test_single_deliver_is_persisted(self) -> None:
        node = _SingleDeliverNode()
        node.name = "single_deliver"
        ctx = _make_linear_ctx()
        await node.run(ctx)
        assert _target_contents(ctx, "downstream_a") == ["payload_a"]
        store = ctx.coordinator.get_deliver_store("downstream_a")
        assert store is not None
        records = store.query_consumable(ctx.coordinator.graph_instance_id, "downstream_a")
        assert records[0].status is DeliverConsumptionStatus.PENDING

    async def test_multiple_delivers_same_next_node(self) -> None:
        node = _MultiDeliverNode()
        node.name = "multi_deliver"
        ctx = _make_linear_ctx()
        await node.run(ctx)
        assert _target_contents(ctx, "target_x") == ["data_1", "data_2"]
        assert _target_contents(ctx, "target_y") == ["data_3"]

    async def test_deliver_with_explicit_ctx(self) -> None:
        node = _DeliverWithCtxNode()
        node.name = "explicit_ctx"
        ctx = _make_linear_ctx()
        await node.run(ctx)
        assert _target_contents(ctx, "explicit_target") == ["explicit_ctx_data"]

    def test_deliver_outside_active_invocation_raises(self) -> None:
        node = _SingleDeliverNode()
        node.name = "single_deliver"
        ctx = _make_linear_ctx()
        with pytest.raises(RuntimeError, match="active node invocation"):
            node.deliver("data", "target", ctx)

    async def test_custom_deliver_override(self) -> None:
        node = _CustomDeliverNode()
        node.name = "custom_deliver"
        ctx = _make_linear_ctx()
        await node.run(ctx)
        assert _target_contents(ctx, "target") == ["custom:data"]


# ── completion: one scheduling wakeup per affected target ────────────────


class TestCompletionDispatch:
    async def test_multiple_delivers_wake_each_target_once(self) -> None:
        node = _MultiDeliverNode()
        node.name = "multi_deliver"
        ctx, dispatch_calls = _make_parallel_ctx()
        await node.run(ctx)
        targets = [target for _source, target in dispatch_calls]
        assert sorted(targets) == ["target_x", "target_y"]
        assert _target_contents(ctx, "target_x") == ["data_1", "data_2"]
        assert _target_contents(ctx, "target_y") == ["data_3"]

    @pytest.mark.parametrize("kind", [SchedulerKind.LINEAR, SchedulerKind.PARALLEL])
    async def test_completion_wakeup_carries_no_delivery_payload(
        self, kind: SchedulerKind
    ) -> None:
        node = _SingleDeliverNode()
        node.name = "single_deliver"
        calls: list[tuple[str, str]] = []
        ctx = GraphContext(
            state=CounterState(),
            runtime=make_runtime(),
            coordinator=make_coordinator(),
            scheduler_kind=kind,
        )
        ctx.set_dispatch_handler(lambda source, target: calls.append((source, target)))
        await node.run(ctx)
        assert len(calls) == 1
        _source, target = calls[0]
        assert target == "downstream_a"
        assert _target_contents(ctx, "downstream_a") == ["payload_a"]


# ── _resolve_default_target limitation ────────────────────────────────────


class _NullNextNodeDeliver(Node[CounterState]):
    """Node that delivers without specifying next_node."""

    async def execute(
        self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput
    ) -> None:
        self.deliver("data", None, ctx)  # next_node=None
        return None


class TestResolveDefaultTargetLimitation:
    async def test_deliver_without_next_node_raises_during_execute(self) -> None:
        node = _NullNextNodeDeliver()
        node.name = "null_next"
        ctx = _make_linear_ctx()
        with pytest.raises(RoutingError, match="graph topology"):
            await node.run(ctx)

    def test_resolve_default_target_raises_directly(self) -> None:
        node = _NullNextNodeDeliver()
        node.name = "null_next"
        ctx = _make_linear_ctx()
        with pytest.raises(RoutingError, match="graph topology"):
            node._resolve_default_target(ctx)

    async def test_resolve_default_target_with_graph(self) -> None:
        """Passing graph= to run() resolves None via default edges."""
        from helpers import AddNode

        from modex_graph import Graph, GraphNode, LinearScheduler

        g: Graph[CounterState] = Graph()
        g.add_node("null_next", _NullNextNodeDeliver())
        g.add_node("downstream", AddNode(amount=1))
        g.add_edge(GraphNode.START, "null_next")
        g.add_edge("null_next", "downstream")
        g.add_edge("downstream", GraphNode.END)
        compiled = g.compile()
        ctx = make_ctx(CounterState())
        scheduler = LinearScheduler(compiled)
        result = await scheduler.run_async(ctx, mode=BootstrapMode.FRESH)
        assert result.count == 1

    async def test_resolve_default_target_no_default_uses_downstream(self) -> None:
        """No default edge → all downstream edges are targets."""
        from helpers import AddNode

        from modex_graph import Graph, GraphNode, LinearScheduler

        g: Graph[CounterState] = Graph()
        g.add_node("null_next", _NullNextNodeDeliver())
        g.add_node("target_a", AddNode(amount=1))
        g.add_edge(GraphNode.START, "null_next")
        g.add_edge("null_next", "target_a")
        g.add_edge("target_a", GraphNode.END)
        compiled = g.compile()
        ctx = make_ctx(CounterState())
        scheduler = LinearScheduler(compiled)
        result = await scheduler.run_async(ctx, mode=BootstrapMode.FRESH)
        assert result.count == 1

    def test_resolve_default_target_strict_multi_edge_raises(self) -> None:
        """Strict policy (default) raises on multiple downstream edges."""
        from helpers import AddNode

        from modex_graph import Graph, GraphNode

        g: Graph[CounterState] = Graph()
        g.add_node("multi", _NullNextNodeDeliver())
        g.add_node("target_a", AddNode(amount=1))
        g.add_node("target_b", AddNode(amount=2))
        g.add_edge(GraphNode.START, "multi")
        g.add_edge("multi", "target_a")
        g.add_edge("multi", "target_b")
        compiled = g.compile()
        node = _NullNextNodeDeliver()
        node.name = "multi"
        node._graph_ref = compiled
        ctx = _make_linear_ctx()
        with pytest.raises(RoutingError, match="downstream targets"):
            node._resolve_default_target(ctx)

    def test_resolve_default_target_graceful_multi_edge_returns_end(self) -> None:
        """Graceful policy returns [END] on multiple downstream edges."""
        from helpers import AddNode

        from modex_graph import Graph, GraphNode

        g: Graph[CounterState] = Graph()
        g.add_node("multi", _NullNextNodeDeliver())
        g.add_node("target_a", AddNode(amount=1))
        g.add_node("target_b", AddNode(amount=2))
        g.add_edge(GraphNode.START, "multi")
        g.add_edge("multi", "target_a")
        g.add_edge("multi", "target_b")
        compiled = g.compile()
        node = _NullNextNodeDeliver()
        node.name = "multi"
        node._graph_ref = compiled
        ctx = _make_linear_ctx()
        assert node._resolve_default_target(ctx, policy="graceful") == [GraphNode.END]

    def test_resolve_default_target_graceful_single_edge_returns_target(self) -> None:
        """Graceful policy with one downstream edge returns that target."""
        from helpers import AddNode

        from modex_graph import Graph, GraphNode

        g: Graph[CounterState] = Graph()
        g.add_node("single", _NullNextNodeDeliver())
        g.add_node("only_down", AddNode(amount=1))
        g.add_edge(GraphNode.START, "single")
        g.add_edge("single", "only_down")
        compiled = g.compile()
        node = _NullNextNodeDeliver()
        node.name = "single"
        node._graph_ref = compiled
        ctx = _make_linear_ctx()
        assert node._resolve_default_target(ctx, policy="graceful") == ["only_down"]


# ── GraphNode.END as next_node ────────────────────────────────────────────


class _DeliverToEndNode(Node[CounterState]):
    """Node that delivers to GraphNode.END."""

    async def execute(
        self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput
    ) -> None:
        self.deliver("terminal_data", GraphNode.END, ctx)
        return None


class TestDeliverToEnd:
    @pytest.mark.parametrize("kind", [SchedulerKind.LINEAR, SchedulerKind.PARALLEL])
    async def test_deliver_to_end_reaches_terminal_once(self, kind: SchedulerKind) -> None:
        from modex_graph import Graph, GraphEngine

        graph: Graph[CounterState] = Graph()
        graph.add_node("deliver_to_end", _DeliverToEndNode())
        graph.add_edge(GraphNode.START, "deliver_to_end")
        graph.add_edge("deliver_to_end", GraphNode.END)
        compiled = graph.compile(scheduler=kind)
        ctx = make_ctx(CounterState())

        await GraphEngine(compiled).run_async(ctx, mode=BootstrapMode.FRESH)

        assert ctx.reached_end is True


# ── upstream_payloads flow: deliver → integrated_input ────────────────────


class _RecordingSinkNode(Node[CounterState]):
    """Records integrated_input on each execute, then delivers to END."""

    def __init__(self) -> None:
        self.seen_inputs: list[IntegratedInput] = []

    async def execute(
        self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput
    ) -> None:
        self.seen_inputs.append(integrated_input)
        self.deliver("sink_done", GraphNode.END, ctx)
        return None


class _SourceNode(Node[CounterState]):
    """Delivers a fixed content to a target."""

    def __init__(self, content: Any, target: str) -> None:
        self.content = content
        self.target = target

    async def execute(
        self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput
    ) -> None:
        self.deliver(self.content, self.target, ctx)
        return None


class TestUpstreamPayloadsFlow:
    """Delivers flow from Node A's deliver → Node B's
    integrated_input under both LINEAR and PARALLEL schedulers.

    ``Node.deliver`` stages data in the target store. Source completion promotes
    it to PENDING, dispatch wakes the target without carrying data, and
    ``Node.run()`` consumes it via ``collect_consumable_delivers``.
    """

    async def test_flow_under_linear(self) -> None:
        """Node A delivers content → Node B receives it via integrated_input."""
        from modex_graph import Graph, LinearScheduler

        sink = _RecordingSinkNode()
        g: Graph[CounterState] = Graph()
        g.add_node("a", _SourceNode(content="from_a", target="b"))
        g.add_node("b", sink)
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", "b")
        g.add_edge("b", GraphNode.END)
        compiled = g.compile()

        ctx = GraphContext(
            state=CounterState(count=0),
            runtime=make_runtime(),
            coordinator=make_coordinator(),
        )
        await LinearScheduler(compiled).run_async(ctx, mode=BootstrapMode.FRESH)

        assert len(sink.seen_inputs) == 1
        integrated = sink.seen_inputs[0]
        assert len(integrated.payloads) == 1
        assert integrated.payloads[0].source_node == compiled.nodes["a"].node_id
        assert integrated.payloads[0].content == "from_a"
        assert integrated.integrated_content == ["from_a"]

    async def test_flow_under_linear_multiple_delivers(self) -> None:
        """Node A stages multiple rows and one wakeup lets B consume both."""
        from modex_graph import Graph, LinearScheduler

        class MultiSourceNode(Node[CounterState]):
            async def execute(
                self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput
            ) -> None:
                self.deliver("first", "b", ctx)
                self.deliver("second", "b", ctx)
                return None

        sink = _RecordingSinkNode()
        g: Graph[CounterState] = Graph()
        g.add_node("a", MultiSourceNode())
        g.add_node("b", sink)
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", "b")
        g.add_edge("b", GraphNode.END)
        compiled = g.compile()

        ctx = make_ctx(CounterState(count=0))
        await LinearScheduler(compiled).run_async(ctx, mode=BootstrapMode.FRESH)

        assert len(sink.seen_inputs) == 1
        integrated = sink.seen_inputs[0]
        assert len(integrated.payloads) == 2
        assert integrated.payloads[0].source_node == compiled.nodes["a"].node_id
        assert integrated.payloads[0].content == "first"
        assert integrated.payloads[1].content == "second"
        assert integrated.integrated_content == ["first", "second"]

    async def test_flow_under_parallel_on_receive(self) -> None:
        """Under PARALLEL + ON_RECEIVE, B reads promoted data from its store."""
        from modex_graph import (
            Graph,
            NodeTrigger,
            ParallelScheduler,
        )

        sink = _RecordingSinkNode()
        g: Graph[CounterState] = Graph()
        g.add_node("a", _SourceNode(content={"data": 42}, target="b"))
        g.add_node("b", sink)
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", "b")
        g.add_edge("b", GraphNode.END)
        compiled = g.compile(
            scheduler=SchedulerKind.PARALLEL,
            default_trigger=NodeTrigger.ON_RECEIVE,
        )

        ctx = GraphContext(
            state=CounterState(count=0),
            runtime=make_runtime(),
            coordinator=make_coordinator(),
            scheduler_kind=SchedulerKind.PARALLEL,
        )
        await ParallelScheduler(compiled).run_async(ctx, mode=BootstrapMode.FRESH)

        assert len(sink.seen_inputs) == 1
        integrated = sink.seen_inputs[0]
        assert len(integrated.payloads) == 1
        assert integrated.payloads[0].source_node == compiled.nodes["a"].node_id
        assert integrated.payloads[0].content == {"data": 42}

    async def test_flow_under_parallel_on_all_preds(self) -> None:
        """Under PARALLEL + ON_ALL_PREDS: two DIFFERENT source nodes deliver
        to the same target → the target receives one IntegratedPayload per
        source via upstream_payloads."""
        from modex_graph import (
            Graph,
            NodeTrigger,
            ParallelScheduler,
        )

        class FanOutNode(Node[CounterState]):
            async def execute(
                self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput
            ) -> None:
                self.deliver("from_a", "b", ctx)
                self.deliver("from_a", "c", ctx)
                return None

        sink = _RecordingSinkNode()
        g: Graph[CounterState] = Graph()
        g.add_node("a", FanOutNode())
        g.add_node("b", _SourceNode(content="from_b", target="d"))
        g.add_node("c", _SourceNode(content="from_c", target="d"))
        g.add_node("d", sink)
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", "b")
        g.add_edge("a", "c")
        g.add_edge("b", "d")
        g.add_edge("c", "d")
        g.add_edge("d", GraphNode.END)
        compiled = g.compile(
            scheduler=SchedulerKind.PARALLEL,
            default_trigger=NodeTrigger.ON_ALL_PREDS,
        )

        ctx = GraphContext(
            state=CounterState(count=0),
            runtime=make_runtime(),
            coordinator=make_coordinator(),
            scheduler_kind=SchedulerKind.PARALLEL,
        )
        await ParallelScheduler(compiled).run_async(ctx, mode=BootstrapMode.FRESH)

        assert len(sink.seen_inputs) == 1
        integrated = sink.seen_inputs[0]
        # Two different sources (b, c) → two IntegratedPayloads.
        assert len(integrated.payloads) == 2
        by_source = {p.source_node: p.content for p in integrated.payloads}
        assert by_source == {
            compiled.nodes["b"].node_id: "from_b",
            compiled.nodes["c"].node_id: "from_c",
        }

    async def test_entry_node_receives_start_payload(self) -> None:
        from modex_graph import Graph, LinearScheduler

        sink = _RecordingSinkNode()
        g: Graph[CounterState] = Graph()
        g.add_node("entry", sink)
        g.add_edge(GraphNode.START, "entry")
        g.add_edge("entry", GraphNode.END)
        compiled = g.compile()

        ctx = make_ctx(CounterState(count=0))
        await LinearScheduler(compiled).run_async(ctx, mode=BootstrapMode.FRESH)

        assert len(sink.seen_inputs) == 1
        integrated = sink.seen_inputs[0]
        assert len(integrated.payloads) == 1
        assert integrated.payloads[0].source_node == compiled.nodes[GraphNode.START].node_id
        assert integrated.integrated_content == [None]
