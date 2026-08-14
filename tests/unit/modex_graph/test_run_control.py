from __future__ import annotations

import asyncio

import pytest
from helpers import CounterState, make_coordinator, make_runtime

from modex_graph import (
    Graph,
    GraphContext,
    GraphDrained,
    GraphNode,
    GraphPersistenceCoordinator,
    GraphRunControl,
    GraphRuntime,
    InMemoryDeliverStore,
    IntegratedInput,
    InvocationStatus,
    LinearScheduler,
    Node,
    NodeInstanceStatus,
    NodeTrigger,
    ParallelScheduler,
    SchedulerKind,
)


def _persistent_coordinator(*node_names: str) -> GraphPersistenceCoordinator:
    coordinator = make_coordinator()
    for node_name in node_names:
        coordinator.register_node(node_name, InMemoryDeliverStore())
    return coordinator


class _DeliverNode(Node[CounterState]):
    def __init__(self, target: str, amount: int = 1) -> None:
        self.target = target
        self.amount = amount

    async def execute(
        self,
        ctx: GraphContext[CounterState],
        integrated_input: IntegratedInput,
    ) -> None:
        ctx.state.count += self.amount
        self.deliver(self.name, self.target, ctx)


class _PauseAfterNodeRuntime(GraphRuntime):
    async def after_node(
        self,
        ctx: GraphContext[CounterState],
        node_name: str,
    ) -> None:
        if node_name == "a":
            ctx.control.request_pause("test")


class _BlockingNode(Node[CounterState]):
    def __init__(self, started: asyncio.Event, release: asyncio.Event) -> None:
        self.started = started
        self.release = release
        self.completed = False

    async def execute(
        self,
        ctx: GraphContext[CounterState],
        integrated_input: IntegratedInput,
    ) -> None:
        self.started.set()
        await self.release.wait()
        self.completed = True
        self.deliver(None, GraphNode.END, ctx)


class _RecordingNode(Node[CounterState]):
    def __init__(self, executed: asyncio.Event | None = None) -> None:
        self.executed = executed
        self.inputs: list[IntegratedInput] = []

    async def execute(
        self,
        ctx: GraphContext[CounterState],
        integrated_input: IntegratedInput,
    ) -> None:
        self.inputs.append(integrated_input)
        if self.executed is not None:
            self.executed.set()
        self.deliver(None, GraphNode.END, ctx)


class TestGraphRunControl:
    def test_contexts_receive_distinct_default_controls(self) -> None:
        first = GraphContext(
            state=CounterState(),
            runtime=make_runtime(),
            coordinator=make_coordinator(),
        )
        second = GraphContext(
            state=CounterState(),
            runtime=make_runtime(),
            coordinator=make_coordinator(),
        )

        assert first.control is not second.control

    def test_pause_and_stop_are_one_way_drain_signals(self) -> None:
        pause_control = GraphRunControl()
        pause_control.request_pause("pause reason")

        assert pause_control.pause_requested is True
        assert pause_control.stop_requested is False
        assert pause_control.drain_reason == "pause reason"
        with pytest.raises(GraphDrained, match="pause reason"):
            pause_control.check()

        stop_control = GraphRunControl()
        stop_control.request_stop("stop reason")

        assert stop_control.pause_requested is False
        assert stop_control.stop_requested is True
        assert stop_control.drain_reason == "stop reason"
        with pytest.raises(GraphDrained, match="stop reason"):
            stop_control.check()

    def test_notify_deliver_sets_injected_wakeup(self) -> None:
        wakeup = asyncio.Event()
        control = GraphRunControl()
        control.set_wakeup(wakeup)

        control.notify_deliver("target")

        assert wakeup.is_set()


class TestLinearSchedulerControl:
    async def test_pause_between_nodes_stops_before_next_node(self) -> None:
        graph: Graph[CounterState] = Graph()
        graph.add_node("a", _DeliverNode("b"))
        graph.add_node("b", _DeliverNode("c", amount=10))
        graph.add_node("c", _DeliverNode(GraphNode.END, amount=100))
        graph.add_edge(GraphNode.START, "a")
        graph.add_edge("a", "b")
        graph.add_edge("b", "c")
        graph.add_edge("c", GraphNode.END)
        compiled = graph.compile()
        node_ids = {name: node.node_id for name, node in compiled.nodes.items()}
        coordinator = _persistent_coordinator(*node_ids.values())
        coordinator.route_deliver(node_ids["a"], "seed", "external", 1)
        ctx = GraphContext(
            state=CounterState(),
            runtime=_PauseAfterNodeRuntime(),
            coordinator=coordinator,
        )

        with pytest.raises(GraphDrained, match="test"):
            await LinearScheduler(compiled).run_async(ctx)

        assert ctx.state.count == 1
        a_record = coordinator.node_state_store.load_latest(node_ids["a"])
        assert a_record is not None
        assert a_record.status == InvocationStatus.COMPLETED
        assert coordinator.collect_consumable_delivers(node_ids["a"], 0) == []
        pending_for_b = coordinator.collect_consumable_delivers(node_ids["b"], 0)
        assert len(pending_for_b) == 1
        assert coordinator.node_state_store.load_latest(node_ids["b"]) is None


class TestParallelSchedulerControl:
    async def test_pause_wakes_scheduler_cancels_running_and_skips_ready(self) -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        blocking = _BlockingNode(started, release)
        queued = _RecordingNode()
        graph: Graph[CounterState] = Graph()
        graph.add_node("blocking", blocking)
        graph.add_node("queued", queued)
        graph.add_edge(GraphNode.START, "blocking")
        graph.add_edge("blocking", "queued")
        graph.add_edge("blocking", GraphNode.END)
        graph.add_edge("queued", GraphNode.END)
        compiled = graph.compile(scheduler=SchedulerKind.PARALLEL)
        node_ids = {name: node.node_id for name, node in compiled.nodes.items()}
        coordinator = _persistent_coordinator(*node_ids.values())
        ctx = GraphContext(
            state=CounterState(),
            runtime=make_runtime(),
            coordinator=coordinator,
            scheduler_kind=SchedulerKind.PARALLEL,
        )
        scheduler = ParallelScheduler(compiled)
        run_task = asyncio.create_task(scheduler.run_async(ctx))
        await asyncio.wait_for(started.wait(), timeout=1)

        queued_id = scheduler._create_instance("queued")
        scheduler._instances[queued_id].status = NodeInstanceStatus.READY
        scheduler._ready.add(queued_id)
        ctx.control.request_pause("test")

        with pytest.raises(GraphDrained, match="test"):
            await asyncio.wait_for(run_task, timeout=1)

        assert blocking.completed is False
        assert queued.inputs == []
        blocking_record = coordinator.node_state_store.load_latest(node_ids["blocking"])
        assert blocking_record is not None
        assert blocking_record.status == InvocationStatus.CRASHED

    async def test_notify_deliver_wakes_and_runs_external_target(self) -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        target_executed = asyncio.Event()
        blocking = _BlockingNode(started, release)
        target = _RecordingNode(target_executed)
        graph: Graph[CounterState] = Graph()
        graph.add_node("blocking", blocking)
        graph.add_node("target", target)
        graph.add_edge(GraphNode.START, "blocking")
        graph.add_edge("blocking", "target")
        graph.add_edge("blocking", GraphNode.END)
        graph.add_edge("target", GraphNode.END)
        compiled = graph.compile(
            scheduler=SchedulerKind.PARALLEL,
            default_trigger=NodeTrigger.ON_RECEIVE,
        )
        node_ids = {name: node.node_id for name, node in compiled.nodes.items()}
        coordinator = _persistent_coordinator(*node_ids.values())
        ctx = GraphContext(
            state=CounterState(),
            runtime=make_runtime(),
            coordinator=coordinator,
            scheduler_kind=SchedulerKind.PARALLEL,
        )
        scheduler = ParallelScheduler(compiled)
        run_task = asyncio.create_task(scheduler.run_async(ctx))
        await asyncio.wait_for(started.wait(), timeout=1)

        coordinator.route_deliver(node_ids["target"], "external payload", "external", 1)
        ctx.control.notify_deliver("target")
        await asyncio.wait_for(target_executed.wait(), timeout=1)
        release.set()
        await asyncio.wait_for(run_task, timeout=1)

        assert len(target.inputs) == 1
        assert target.inputs[0].integrated_content == ["external payload"]
