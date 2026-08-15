# ruff: noqa: ANN401
"""Shared test state types + node helpers for modex_graph unit tests."""

from __future__ import annotations

from typing import Any

from modex_graph import (
    CompiledGraph,
    GraphContext,
    GraphInstance,
    GraphInstanceStatus,
    GraphMetadata,
    GraphPersistenceCoordinator,
    GraphRuntime,
    GraphState,
    InMemoryNodeStateStore,
    IntegratedInput,
    Node,
    NullDeliverStoreFactory,
    NullGraphInstanceStore,
    create_null_coordinator,
)


class _AutoRegisterCoordinator(GraphPersistenceCoordinator):
    """Test-only coordinator that auto-registers nodes on
    collect_consumable_delivers and route_deliver.

    Simplifies test setup so tests don't need to explicitly call
    register_node before node.run(). NOT for production use.
    """

    def collect_consumable_delivers(
        self, node_id: str, invocation_id: int
    ) -> list[Any]:
        if self.get_deliver_store(node_id) is None:
            self.register_node(node_id)
        return super().collect_consumable_delivers(node_id, invocation_id)

    def route_deliver(
        self,
        target_node_id: str,
        content: Any,
        source_node_id: str,
        source_invocation_id: int,
        source_node_name: str | None = None,
        stage: bool = False,
    ) -> int:
        if self.get_deliver_store(target_node_id) is None:
            self.register_node(target_node_id)
        return super().route_deliver(
            target_node_id,
            content,
            source_node_id,
            source_invocation_id,
            source_node_name,
            stage,
        )


class TrackingRuntime(GraphRuntime):
    """Runtime that records ``before_node`` / ``after_node`` / ``emit`` calls."""

    def __init__(self) -> None:
        self.before_calls: list[str] = []
        self.after_calls: list[str] = []
        self.emit_calls: list[tuple[str, Any]] = []

    async def before_node(self, ctx: GraphContext[Any], node_name: str) -> None:
        self.before_calls.append(node_name)

    async def after_node(self, ctx: GraphContext[Any], node_name: str) -> None:
        self.after_calls.append(node_name)

    async def emit(self, event_type: str, data: Any, ctx: GraphContext[Any]) -> None:
        self.emit_calls.append((event_type, data))


class CounterState(GraphState):
    """Simple state with a counter + message list for testing."""

    count: int = 0
    name: str = ""
    messages: list[str] = []


class AddNode(Node[CounterState]):
    def __init__(self, amount: int = 1) -> None:
        self.amount = amount

    async def execute(
        self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput
    ) -> None:
        ctx.state.count += self.amount
        self.deliver(None, None, ctx)
        return None


class AsyncAddNode(Node[CounterState]):
    """Async node that increments count by `amount`, delivers to default target."""

    def __init__(self, amount: int = 1) -> None:
        self.amount = amount

    async def execute(
        self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput
    ) -> None:
        ctx.state.count += self.amount
        self.deliver(None, None, ctx)
        return None


class InterruptNode(Node[CounterState]):
    """Node that calls ctx.interrupt(value) to suspend."""

    def __init__(self, value: Any = "interrupted") -> None:
        self.value = value

    async def execute(
        self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput
    ) -> None:
        ctx.interrupt(self.value)
        return None


class RecordNameNode(Node[CounterState]):
    def __init__(self, label: str | None = None) -> None:
        self.label = label

    async def execute(
        self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput
    ) -> None:
        label = self.label if self.label is not None else self.name
        ctx.state.messages.append(label)
        self.deliver(None, None, ctx)
        return None


def make_runtime() -> GraphRuntime:
    """Return a default no-op GraphRuntime."""
    return GraphRuntime()


def make_coordinator(
    node_names: tuple[str, ...] = (),
) -> GraphPersistenceCoordinator:
    """Build a Null-strategy coordinator for tests.

    Uses NullGraphInstanceStore + NullNodeStateStore + NullDeliverStoreFactory
    (rule 15 Null strategy — no persistence). Returns an
    ``_AutoRegisterCoordinator`` that auto-registers nodes on
    ``collect_consumable_delivers``, so tests don't need explicit
    ``register_node`` calls. Pass ``node_names`` to pre-register specific nodes.
    """
    coordinator = _AutoRegisterCoordinator(
        graph_instance_id=0,
        instance_store=NullGraphInstanceStore(),
        node_state_store=InMemoryNodeStateStore(0),
        default_deliver_store_factory=NullDeliverStoreFactory(),
    )
    for name in node_names:
        coordinator.register_node(name)
    return coordinator


def register_graph_nodes(
    coordinator: GraphPersistenceCoordinator,
    compiled: CompiledGraph[Any],
) -> None:
    """Register all nodes from a compiled graph with the coordinator."""
    for node in compiled.nodes.values():
        coordinator.register_node(node.node_id)


def make_ctx(
    state: CounterState | None = None,
    *,
    coordinator: GraphPersistenceCoordinator | None = None,
    node_names: tuple[str, ...] = (),
) -> GraphContext[CounterState]:
    """Build a GraphContext with a CounterState + no-op runtime + coordinator."""
    coord = coordinator if coordinator is not None else make_coordinator(node_names)
    ctx = GraphContext(
        state=state if state is not None else CounterState(),
        runtime=make_runtime(),
        coordinator=coord,
    )
    ctx.set_dispatch_handler(lambda _src, _tgt: None)
    return ctx


def make_graph_metadata(
    gid: int = 1,
    spec_id: int = 999,
    status: GraphInstanceStatus = GraphInstanceStatus.RUNNING,
    parent_instance_id: int | None = None,
    parent_node: str | None = None,
) -> GraphMetadata:
    """Build a ``GraphMetadata`` value object for tests."""
    return GraphMetadata(
        graph_instance_id=gid,
        spec_id=spec_id,
        parent_instance_id=parent_instance_id,
        parent_node=parent_node,
        status=status,
    )


def make_graph_instance(
    gid: int = 1,
    spec_id: int = 999,
    status: GraphInstanceStatus = GraphInstanceStatus.RUNNING,
    parent_instance_id: int | None = None,
    parent_node: str | None = None,
) -> GraphInstance:
    """Build a ``GraphInstance`` with ``GraphMetadata`` + null coordinator."""
    metadata = make_graph_metadata(
        gid=gid,
        spec_id=spec_id,
        status=status,
        parent_instance_id=parent_instance_id,
        parent_node=parent_node,
    )
    return GraphInstance(metadata, create_null_coordinator(gid))
