# ruff: noqa: ANN401
"""Shared test state types + node helpers for modex_graph unit tests."""

from __future__ import annotations

from typing import Annotated, Any

from modex_graph import (
    CompiledGraph,
    GraphContext,
    GraphInstance,
    GraphInstanceStatus,
    GraphMetadata,
    GraphNode,
    GraphPersistenceCoordinator,
    GraphRuntime,
    GraphState,
    IntegratedInput,
    InvocationContext,
    LastValue,
    Node,
    NodeResult,
    NullDeliverStoreFactory,
    NullGraphMetadataStore,
    NullNodeStateFactory,
    ReducerChannel,
    create_null_coordinator,
)


class _AutoRegisterCoordinator(GraphPersistenceCoordinator):
    """Test-only coordinator that auto-registers nodes on begin_invocation
    and route_deliver.

    Simplifies test setup so tests don't need to explicitly call
    register_node before node.run(). NOT for production use — production
    code requires explicit registration per the coordinator contract.
    """

    def begin_invocation(self, node_name: str) -> InvocationContext:
        if self.get_deliver_store(node_name) is None:
            self.register_node(node_name)
        return super().begin_invocation(node_name)

    def route_deliver(
        self,
        target_node: str,
        content: Any,
        source_node: str,
        source_invocation_id: int,
    ) -> int | None:
        if target_node != GraphNode.END and self.get_deliver_store(target_node) is None:
            self.register_node(target_node)
        return super().route_deliver(target_node, content, source_node, source_invocation_id)


class TrackingRuntime(GraphRuntime):
    """Runtime that records ``before_node`` / ``after_node`` / ``emit`` calls.

    Used by Task 08 tests to verify concurrent hook invocation is safe
    (no crash, no race). The lists use ``list.append`` which is GIL-atomic
    in CPython, so concurrent ``asyncio.gather`` tasks can append safely
    without an explicit lock — the append runs in a synchronous section
    between await points.
    """

    def __init__(self) -> None:
        self.before_calls: list[str] = []
        self.after_calls: list[str] = []
        self.emit_calls: list[tuple[str, Any]] = []

    async def before_node(self, ctx: GraphContext[Any], node_name: str) -> None:
        self.before_calls.append(node_name)

    async def after_node(self, ctx: GraphContext[Any], node_name: str, result: Any) -> None:
        self.after_calls.append(node_name)

    async def emit(self, event_type: str, data: Any, ctx: GraphContext[Any]) -> None:
        self.emit_calls.append((event_type, data))


class CounterState(GraphState):
    """Simple state with a counter + message list for testing."""

    count: Annotated[int, LastValue] = 0
    name: Annotated[str, LastValue] = ""
    messages: Annotated[list[str], ReducerChannel(reducer=lambda a, b: a + b)] = []


class AddNode(Node[CounterState]):
    """Sync node that increments count by `amount`, delivers to default target."""

    def __init__(self, amount: int = 1) -> None:
        self.amount = amount

    def execute(
        self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput
    ) -> NodeResult:
        ctx.state.count += self.amount
        self.deliver(None, None, ctx)
        return NodeResult()


class AsyncAddNode(Node[CounterState]):
    """Async node that increments count by `amount`, delivers to default target."""

    def __init__(self, amount: int = 1) -> None:
        self.amount = amount

    async def execute(
        self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput
    ) -> NodeResult:
        ctx.state.count += self.amount
        self.deliver(None, None, ctx)
        return NodeResult()


class InterruptNode(Node[CounterState]):
    """Node that calls ctx.interrupt(value) to suspend."""

    def __init__(self, value: Any = "interrupted") -> None:
        self.value = value

    def execute(
        self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput
    ) -> NodeResult:
        ctx.interrupt(self.value)
        # Unreachable — interrupt raises.
        return NodeResult()


class RecordNameNode(Node[CounterState]):
    """Node that records its name into state.messages via state_update."""

    def __init__(self, label: str | None = None) -> None:
        self.label = label

    def execute(
        self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput
    ) -> NodeResult:
        label = self.label if self.label is not None else self.name
        return NodeResult(state_update={"messages": [label]})


def make_runtime() -> GraphRuntime:
    """Return a default no-op GraphRuntime."""
    return GraphRuntime()


def make_coordinator(
    node_names: tuple[str, ...] = (),
) -> GraphPersistenceCoordinator:
    """Build a Null-strategy coordinator for tests.

    Uses NullGraphMetadataStore + NullNodeStateFactory + NullDeliverStoreFactory
    (rule 15 Null strategy — no persistence). Returns an
    ``_AutoRegisterCoordinator`` that auto-registers nodes on
    ``begin_invocation``, so tests don't need explicit ``register_node``
    calls. Pass ``node_names`` to pre-register specific nodes.
    """
    coordinator = _AutoRegisterCoordinator(
        graph_instance_id=0,
        graph_metadata_store=NullGraphMetadataStore(),
        default_node_state_factory=NullNodeStateFactory(),
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
    for name in compiled.nodes:
        coordinator.register_node(name)


def make_ctx(
    state: CounterState | None = None,
    *,
    coordinator: GraphPersistenceCoordinator | None = None,
    node_names: tuple[str, ...] = (),
) -> GraphContext[CounterState]:
    """Build a GraphContext with a CounterState + no-op runtime + coordinator.

    Registers a no-op dispatch handler so ``Node._submit`` can call
    ``ctx.dispatch()`` without a RuntimeError. Tests that need to verify
    dispatch calls should register their own recording handler via
    ``ctx.set_dispatch_handler(...)`` (overwrites the no-op).

    A Null-strategy coordinator is created if none is passed. Pass
    ``node_names`` to auto-register nodes so ``Node.run()`` can call
    ``begin_invocation`` without RoutingError.
    """
    coord = coordinator if coordinator is not None else make_coordinator(node_names)
    ctx = GraphContext(
        state=state if state is not None else CounterState(),
        runtime=make_runtime(),
        coordinator=coord,
    )
    ctx.set_dispatch_handler(lambda _src, _tgt, _update: None)
    return ctx


def make_graph_metadata(
    gid: int = 1,
    spec_id: int = 999,
    status: GraphInstanceStatus = GraphInstanceStatus.RUNNING,
    parent_instance_id: int | None = None,
    parent_node: str | None = None,
) -> GraphMetadata:
    """Build a ``GraphMetadata`` value object for tests.

    Scheduler bookkeeping fields (``instance_seq``, ``iteration_count``,
    ``activated_sources``, ``pending_dispatches``) are zeroed/empty —
    suitable for identity + status tests.
    """
    return GraphMetadata(
        graph_instance_id=gid,
        spec_id=spec_id,
        parent_instance_id=parent_instance_id,
        parent_node=parent_node,
        status=status,
        instance_seq=0,
        iteration_count=0,
        activated_sources={},
        pending_dispatches={},
    )


def make_graph_instance(
    gid: int = 1,
    spec_id: int = 999,
    status: GraphInstanceStatus = GraphInstanceStatus.RUNNING,
    parent_instance_id: int | None = None,
    parent_node: str | None = None,
) -> GraphInstance:
    """Build a ``GraphInstance`` with ``GraphMetadata`` + null coordinator.

    The coordinator is a Null-strategy ``create_null_coordinator(gid)``
    — suitable for tests that need a ``GraphInstance`` for property access
    or method delegation, without persistence side effects.
    """
    metadata = make_graph_metadata(
        gid=gid,
        spec_id=spec_id,
        status=status,
        parent_instance_id=parent_instance_id,
        parent_node=parent_node,
    )
    return GraphInstance(metadata, create_null_coordinator(gid))
