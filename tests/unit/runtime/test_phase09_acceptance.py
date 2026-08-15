from __future__ import annotations

import asyncio

from bot.service.stale_instance_sweeper import StaleInstanceSweeper
from pydantic import BaseModel

from modex_agent.orchestration import GraphOrchestrator
from modex_agent.runtime.constants import EXECUTOR_PROCESS_ID_KEY
from modex_agent.runtime.process_identity import ProcessIdentity
from modex_agent.runtime.process_registry import SingletonProcessRegistry
from modex_graph import (
    CoordinatorFactory,
    DefaultGraphState,
    EdgeSpec,
    GraphContext,
    GraphInstanceStatus,
    GraphInstanceStore,
    GraphNode,
    GraphPersistenceCoordinator,
    GraphSpec,
    InMemoryGraphInstanceStore,
    InMemoryGraphSpecStore,
    InMemoryNodeStateStore,
    IntegratedInput,
    Node,
    NodeFactory,
    NodeRegistry,
    NodeSpec,
    NullDeliverStoreFactory,
)


class _BlockingRecoveryNode(Node[DefaultGraphState]):
    def __init__(self, entered: asyncio.Event, release: asyncio.Event) -> None:
        self._entered = entered
        self._release = release

    async def execute(
        self,
        ctx: GraphContext[DefaultGraphState],
        integrated_input: IntegratedInput,
    ) -> None:
        self._entered.set()
        await self._release.wait()
        self.deliver(None, GraphNode.END, ctx)


class _BlockingRecoveryFactory(NodeFactory):
    def __init__(self, entered: asyncio.Event, release: asyncio.Event) -> None:
        self._entered = entered
        self._release = release

    def create(self, spec: NodeSpec) -> Node[DefaultGraphState]:
        return _BlockingRecoveryNode(self._entered, self._release)

    def config_schema(self) -> type[BaseModel] | None:
        return None


class _InMemoryCoordinatorFactory(CoordinatorFactory):
    def __init__(self) -> None:
        self._node_stores: dict[int, InMemoryNodeStateStore] = {}

    def create(
        self,
        graph_instance_id: int,
        instance_store: GraphInstanceStore,
    ) -> GraphPersistenceCoordinator:
        node_store = self._node_stores.get(graph_instance_id)
        if node_store is None:
            node_store = InMemoryNodeStateStore(graph_instance_id)
            self._node_stores[graph_instance_id] = node_store
        return GraphPersistenceCoordinator(
            graph_instance_id=graph_instance_id,
            instance_store=instance_store,
            node_state_store=node_store,
            default_deliver_store_factory=NullDeliverStoreFactory(),
        )


async def test_process_death_sweeps_then_explicit_recovery_runs_instance() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    previous_identity = ProcessIdentity()
    recovery_identity = ProcessIdentity()
    previous_process_id = previous_identity.process_id
    recovery_process_id = recovery_identity.process_id
    assert previous_process_id != recovery_process_id

    registry = SingletonProcessRegistry(recovery_identity)
    registry.register(previous_process_id)
    instance_store = InMemoryGraphInstanceStore()
    spec_store = InMemoryGraphSpecStore()
    node_registry = NodeRegistry()
    node_registry.register("blocking", _BlockingRecoveryFactory(entered, release))
    orchestrator = GraphOrchestrator(
        node_registry=node_registry,
        state_classes={"default": DefaultGraphState},
        spec_store=spec_store,
        instance_store=instance_store,
        coordinator_factory=_InMemoryCoordinatorFactory(),
        process_identity=recovery_identity,
    )
    spec_id = spec_store.save(
        GraphSpec(
            name="phase09_process_death",
            nodes=[NodeSpec(name="entry", node_type="blocking")],
            edges=[
                EdgeSpec(source=GraphNode.START, target="entry"),
                EdgeSpec(source="entry", target=GraphNode.END),
            ],
            state_class="default",
        )
    )
    graph_instance_id = await orchestrator.create_instance(spec_id)
    instance_store.update_attrs(
        graph_instance_id,
        {EXECUTOR_PROCESS_ID_KEY: previous_process_id},
    )
    instance_store.update_status(graph_instance_id, GraphInstanceStatus.RUNNING)

    registry.unregister(previous_process_id)
    swept = await StaleInstanceSweeper(instance_store, registry).sweep()

    crashed = instance_store.load(graph_instance_id)
    assert swept == [graph_instance_id]
    assert crashed is not None
    assert crashed.status is GraphInstanceStatus.CRASHED
    assert not entered.is_set()

    recovery_task = asyncio.create_task(orchestrator.recover_crashed())
    await asyncio.wait_for(entered.wait(), timeout=5.0)

    running = instance_store.load(graph_instance_id)
    assert running is not None
    assert running.status is GraphInstanceStatus.RUNNING
    assert running.attrs[EXECUTOR_PROCESS_ID_KEY] == recovery_process_id

    release.set()
    assert await recovery_task == [graph_instance_id]

    completed = instance_store.load(graph_instance_id)
    assert completed is not None
    assert completed.status is GraphInstanceStatus.COMPLETED
