from __future__ import annotations

from typing import Any

import pytest
from helpers import CounterState, TrackingRuntime, make_coordinator, make_runtime

from modex_graph import (
    DeliverConsumptionStatus,
    Graph,
    GraphContext,
    GraphInstanceStatus,
    GraphMetadata,
    GraphNode,
    GraphPersistenceCoordinator,
    GraphRuntime,
    InMemoryDeliverStoreFactory,
    InMemoryGraphInstanceStore,
    InMemoryNodeStateStore,
    IntegratedInput,
    InvocationContext,
    LinearScheduler,
    Node,
    SchedulerKind,
)


class RecordingNode(Node[CounterState]):
    def __init__(
        self,
        label: str,
        target: str,
        executions: list[str],
        *,
        payload: str | None = None,
    ) -> None:
        self._label = label
        self._target = target
        self._executions = executions
        self._payload = payload
        self.inputs: list[Any] = []

    async def execute(
        self,
        ctx: GraphContext[CounterState],
        integrated_input: IntegratedInput,
    ) -> None:
        self._executions.append(self._label)
        self.inputs.append(integrated_input.integrated_content)
        ctx.state.count += 1
        self.deliver(self._payload, self._target, ctx)


class FailAfterNodeRuntime(GraphRuntime):
    def __init__(self, node_name: str) -> None:
        self._node_name = node_name
        self._failed = False

    async def after_node(self, ctx: GraphContext[Any], node_name: str) -> None:
        if node_name == self._node_name and not self._failed:
            self._failed = True
            raise RuntimeError(f"failed after {node_name}")


class FailCompleteOnceNodeStateStore(InMemoryNodeStateStore):
    def __init__(self, graph_instance_id: int, node_id: str) -> None:
        super().__init__(graph_instance_id)
        self._node_id = node_id
        self._failed = False

    def complete_invocation(
        self,
        invocation: InvocationContext,
        state: dict[str, Any],
    ) -> None:
        if invocation.node_id == self._node_id and not self._failed:
            self._failed = True
            raise RuntimeError(f"complete failed for {invocation.node_id}")
        super().complete_invocation(invocation, state)


class RingNode(Node[CounterState]):
    def __init__(
        self,
        label: str,
        next_node: str,
        terminal_count: int,
        executions: list[str],
        *,
        crash_on_execution: int | None = None,
    ) -> None:
        self._label = label
        self._next_node = next_node
        self._terminal_count = terminal_count
        self._executions = executions
        self._crash_on_execution = crash_on_execution
        self._execution_count = 0

    async def execute(
        self,
        ctx: GraphContext[CounterState],
        integrated_input: IntegratedInput,
    ) -> None:
        self._execution_count += 1
        self._executions.append(self._label)
        if self._execution_count == self._crash_on_execution:
            raise RuntimeError(f"{self._label} crashed")
        ctx.state.count += 1
        target = GraphNode.END if ctx.state.count >= self._terminal_count else self._next_node
        self.deliver(self._label, target, ctx)


def make_persistent_coordinator(
    graph_instance_id: int,
    node_names: tuple[str, ...],
    *,
    node_state_store: InMemoryNodeStateStore | None = None,
) -> GraphPersistenceCoordinator:
    instance_store = InMemoryGraphInstanceStore()
    instance_store.save(
        GraphMetadata(
            graph_instance_id=graph_instance_id,
            spec_id=0,
            parent_instance_id=None,
            parent_node=None,
            status=GraphInstanceStatus.RUNNING,
        )
    )
    coordinator = GraphPersistenceCoordinator(
        graph_instance_id=graph_instance_id,
        instance_store=instance_store,
        node_state_store=(
            node_state_store
            if node_state_store is not None
            else InMemoryNodeStateStore(graph_instance_id)
        ),
        default_deliver_store_factory=InMemoryDeliverStoreFactory(),
    )
    for node_name in node_names:
        coordinator.register_node(node_name)
    return coordinator


def make_linear_context(
    coordinator: GraphPersistenceCoordinator,
    *,
    runtime: GraphRuntime | None = None,
) -> GraphContext[CounterState]:
    return GraphContext(
        state=CounterState(),
        runtime=runtime if runtime is not None else make_runtime(),
        coordinator=coordinator,
        scheduler_kind=SchedulerKind.LINEAR,
    )


async def test_recovery_starts_after_completed_linear_prefix() -> None:
    executions: list[str] = []
    graph: Graph[CounterState] = Graph()
    graph.add_node("a", RecordingNode("a", "b", executions))
    graph.add_node("b", RecordingNode("b", "c", executions))
    graph.add_node("c", RecordingNode("c", GraphNode.END, executions))
    graph.add_edge(GraphNode.START, "a")
    graph.add_edge("a", "b")
    graph.add_edge("b", "c")
    graph.add_edge("c", GraphNode.END)
    compiled = graph.compile()
    coordinator = make_persistent_coordinator(
        3601, tuple(node.node_id for node in compiled.nodes.values())
    )

    with pytest.raises(RuntimeError, match="failed after b"):
        await LinearScheduler(compiled).run_async(
            make_linear_context(coordinator, runtime=FailAfterNodeRuntime("b"))
        )

    await LinearScheduler(compiled).run_async(make_linear_context(coordinator))

    assert executions == ["a", "b", "c"]


async def test_pending_deliver_recovers_target_with_no_invocation() -> None:
    executions: list[str] = []
    source = RecordingNode("a", "b", executions, payload="ready")
    target = RecordingNode("b", GraphNode.END, executions)
    graph: Graph[CounterState] = Graph()
    graph.add_node("a", source)
    graph.add_node("b", target)
    graph.add_edge(GraphNode.START, "a")
    graph.add_edge("a", "b")
    graph.add_edge("b", GraphNode.END)
    compiled = graph.compile()
    coordinator = make_persistent_coordinator(
        3602, tuple(node.node_id for node in compiled.nodes.values())
    )

    with pytest.raises(RuntimeError, match="failed after a"):
        await LinearScheduler(compiled).run_async(
            make_linear_context(coordinator, runtime=FailAfterNodeRuntime("a"))
        )

    assert coordinator.node_state_store.load_latest(compiled.nodes["b"].node_id) is None
    await LinearScheduler(compiled).run_async(make_linear_context(coordinator))

    assert executions == ["a", "b"]
    assert target.inputs == [["ready"]]


async def test_pending_deliver_is_witness_for_disconnected_target() -> None:
    executions: list[str] = []
    graph: Graph[CounterState] = Graph()
    graph.add_node("a", RecordingNode("a", GraphNode.END, executions))
    target = RecordingNode("b", GraphNode.END, executions)
    graph.add_node("b", target)
    graph.add_edge(GraphNode.START, "a")
    graph.add_edge("a", GraphNode.END)
    graph.add_edge("b", GraphNode.END)
    compiled = graph.compile()
    coordinator = make_persistent_coordinator(
        3606, tuple(node.node_id for node in compiled.nodes.values())
    )
    coordinator.route_deliver(compiled.nodes["b"].node_id, "external", "external", 0)

    await LinearScheduler(compiled).run_async(make_linear_context(coordinator))

    assert executions == ["b"]
    assert target.inputs == [["external"]]


async def test_submit_persists_deliver_before_completion_failure() -> None:
    graph_instance_id = 3603
    executions: list[str] = []
    graph: Graph[CounterState] = Graph()
    graph.add_node("a", RecordingNode("a", "b", executions, payload="persisted"))
    graph.add_node("b", RecordingNode("b", GraphNode.END, executions))
    graph.add_edge(GraphNode.START, "a")
    graph.add_edge("a", "b")
    graph.add_edge("b", GraphNode.END)
    compiled = graph.compile()
    store = FailCompleteOnceNodeStateStore(graph_instance_id, compiled.nodes["a"].node_id)
    coordinator = make_persistent_coordinator(
        graph_instance_id,
        tuple(node.node_id for node in compiled.nodes.values()),
        node_state_store=store,
    )

    with pytest.raises(RuntimeError, match="complete failed for node_"):
        await LinearScheduler(compiled).run_async(make_linear_context(coordinator))

    pending = coordinator.collect_consumable_delivers(compiled.nodes["b"].node_id, 0)
    assert [record.content for record in pending] == ["persisted"]
    assert all(record.status == DeliverConsumptionStatus.PENDING for record in pending)


async def test_recovery_delivers_old_and_retried_payload_at_least_once() -> None:
    graph_instance_id = 3604
    executions: list[str] = []
    source = RecordingNode("a", "b", executions, payload="from-a")
    target = RecordingNode("b", GraphNode.END, executions)
    graph: Graph[CounterState] = Graph()
    graph.add_node("a", source)
    graph.add_node("b", target)
    graph.add_edge(GraphNode.START, "a")
    graph.add_edge("a", "b")
    graph.add_edge("b", GraphNode.END)
    compiled = graph.compile()
    coordinator = make_persistent_coordinator(
        graph_instance_id,
        tuple(node.node_id for node in compiled.nodes.values()),
        node_state_store=FailCompleteOnceNodeStateStore(
            graph_instance_id, compiled.nodes["a"].node_id
        ),
    )

    with pytest.raises(RuntimeError, match="complete failed for node_"):
        await LinearScheduler(compiled).run_async(make_linear_context(coordinator))

    await LinearScheduler(compiled).run_async(make_linear_context(coordinator))

    assert executions == ["a", "a", "b"]
    assert target.inputs == [["from-a", "from-a"]]


async def test_ring_recovery_uses_latest_non_terminal_version_head() -> None:
    executions: list[str] = []
    node_a = RingNode("a", "b", 5, executions)
    node_b = RingNode("b", "a", 5, executions, crash_on_execution=2)
    graph: Graph[CounterState] = Graph()
    graph.add_node("a", node_a)
    graph.add_node("b", node_b)
    graph.add_edge(GraphNode.START, "a")
    graph.add_edge("a", "b")
    graph.add_edge("b", "a")
    graph.add_edge("a", GraphNode.END)
    graph.add_edge("b", GraphNode.END)
    compiled = graph.compile(cycle_detection="off")
    coordinator = make_persistent_coordinator(
        3605, tuple(node.node_id for node in compiled.nodes.values())
    )

    with pytest.raises(RuntimeError, match="b crashed"):
        await LinearScheduler(compiled).run_async(make_linear_context(coordinator))

    recovery_runtime = TrackingRuntime()
    recovered = make_linear_context(coordinator, runtime=recovery_runtime)
    await LinearScheduler(compiled).run_async(recovered)

    assert recovery_runtime.before_calls == ["b", "a", GraphNode.END]
    assert executions == ["a", "b", "a", "b", "b", "a"]
    assert recovered.state.count == 5


async def test_null_coordinator_still_starts_from_entry() -> None:
    executions: list[str] = []
    graph: Graph[CounterState] = Graph()
    graph.add_node("a", RecordingNode("a", "b", executions))
    graph.add_node("b", RecordingNode("b", GraphNode.END, executions))
    graph.add_edge(GraphNode.START, "a")
    graph.add_edge("a", "b")
    graph.add_edge("b", GraphNode.END)
    compiled = graph.compile()

    await LinearScheduler(compiled).run_async(make_linear_context(make_coordinator(("a", "b"))))

    assert executions == ["a", "b"]
