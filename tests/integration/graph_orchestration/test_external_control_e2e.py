# ruff: noqa: ANN401, S101

"""External graph control and recovery contracts across persistence tiers."""

from __future__ import annotations

import asyncio
import sqlite3
import warnings
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import BaseModel

from modex_agent.orchestration import GraphOrchestrator, SqliteCoordinatorFactory
from modex_graph import (
    CoordinatorFactory,
    DeliverConsumptionStatus,
    DeliverStore,
    DeliverStoreFactory,
    EdgeSpec,
    GraphContext,
    GraphInstanceStatus,
    GraphInstanceStore,
    GraphNode,
    GraphPersistenceCoordinator,
    GraphSpec,
    GraphState,
    InMemoryDeliverStore,
    InMemoryGraphInstanceStore,
    InMemoryGraphSpecStore,
    InMemoryNodeStateStore,
    IntegratedInput,
    InvocationStatus,
    Node,
    NodeFactory,
    NodeRegistry,
    NodeSpec,
    NodeTrigger,
    NullCoordinatorFactory,
    NullGraphInstanceStore,
    SchedulerKind,
    SqliteGraphInstanceStore,
    SqliteGraphSpecStore,
)

pytestmark = pytest.mark.integration

_TIMEOUT = 2.0


class RecoveryState(GraphState):
    count: int = 0
    steps: list[str] = []


class _NodeFactory(NodeFactory):
    def __init__(self, constructors: Mapping[str, Callable[[], Node[Any]]]) -> None:
        self._constructors = constructors

    def create(self, spec: NodeSpec) -> Node[Any]:
        return self._constructors[spec.name]()

    def config_schema(self) -> type[BaseModel] | None:
        return None


def _registry(constructors: Mapping[str, Callable[[], Node[Any]]]) -> NodeRegistry:
    registry = NodeRegistry()
    registry.register("e2e", _NodeFactory(constructors))
    return registry


def _node(name: str, *, trigger: NodeTrigger | None = None) -> NodeSpec:
    return NodeSpec(name=name, node_type="e2e", trigger=trigger)


def _sqlite_orchestrator(
    connection: sqlite3.Connection,
    constructors: Mapping[str, Callable[[], Node[Any]]],
) -> tuple[GraphOrchestrator, SqliteGraphInstanceStore, SqliteGraphSpecStore]:
    instance_store = SqliteGraphInstanceStore(connection)
    spec_store = SqliteGraphSpecStore(connection)
    orchestrator = GraphOrchestrator(
        node_registry=_registry(constructors),
        state_classes={"recovery": RecoveryState},
        spec_store=spec_store,
        instance_store=instance_store,
        coordinator_factory=SqliteCoordinatorFactory(connection),
    )
    return orchestrator, instance_store, spec_store


def _single_instance_id(
    store: GraphInstanceStore,
    status: GraphInstanceStatus,
) -> int:
    instances = store.load_by_status(status)
    assert len(instances) == 1
    return instances[0].graph_instance_id


def _status(store: GraphInstanceStore, graph_instance_id: int) -> GraphInstanceStatus:
    metadata = store.load(graph_instance_id)
    assert metadata is not None
    return metadata.status


class _EmitNode(Node[RecoveryState]):
    def __init__(
        self,
        label: str,
        targets: tuple[str, ...],
        executions: list[str],
        *,
        payload: object | None = None,
    ) -> None:
        self._label = label
        self._targets = targets
        self._executions = executions
        self._payload = label if payload is None else payload

    async def execute(
        self,
        ctx: GraphContext[RecoveryState],
        integrated_input: IntegratedInput,
    ) -> None:
        self._executions.append(self._label)
        ctx.state.steps.append(self._label)
        for target in self._targets:
            self.deliver(self._payload, target, ctx)


class _BlockingNode(Node[RecoveryState]):
    def __init__(
        self,
        label: str,
        started: asyncio.Event,
        executions: list[str],
        should_block: Callable[[], bool],
    ) -> None:
        self._label = label
        self._started = started
        self._executions = executions
        self._should_block = should_block

    async def execute(
        self,
        ctx: GraphContext[RecoveryState],
        integrated_input: IntegratedInput,
    ) -> None:
        self._executions.append(self._label)
        if self._should_block():
            self._started.set()
            await asyncio.Event().wait()
        ctx.state.steps.append(self._label)
        self.deliver(self._label, GraphNode.END, ctx)


class _CaptureNode(Node[RecoveryState]):
    def __init__(
        self,
        label: str,
        executions: list[str],
        inputs: list[list[object]],
        *,
        crash_once: list[bool] | None = None,
    ) -> None:
        self._label = label
        self._executions = executions
        self._inputs = inputs
        self._crash_once = crash_once

    async def execute(
        self,
        ctx: GraphContext[RecoveryState],
        integrated_input: IntegratedInput,
    ) -> None:
        self._executions.append(self._label)
        self._inputs.append(list(integrated_input.integrated_content))
        if self._crash_once is not None and self._crash_once[0]:
            self._crash_once[0] = False
            raise RuntimeError(f"{self._label} crashed")
        ctx.state.steps.append(self._label)
        self.deliver(self._label, GraphNode.END, ctx)


class _FailAfterSubmitNode(_EmitNode):
    def __init__(
        self,
        label: str,
        target: str,
        executions: list[str],
        fail_once: list[bool],
    ) -> None:
        super().__init__(label, (target,), executions, payload="from-a")
        self._fail_once = fail_once

    def submit(self, ctx: GraphContext[RecoveryState]) -> None:
        super().submit(ctx)
        if self._fail_once[0]:
            self._fail_once[0] = False
            raise RuntimeError("source crashed after submit")


class _RingNode(Node[RecoveryState]):
    def __init__(
        self,
        label: str,
        target: str,
        terminal_count: int,
        executions: list[str],
        execution_counts: dict[str, int],
        crash_started: asyncio.Event,
    ) -> None:
        self._label = label
        self._target = target
        self._terminal_count = terminal_count
        self._executions = executions
        self._execution_counts = execution_counts
        self._crash_started = crash_started

    async def execute(
        self,
        ctx: GraphContext[RecoveryState],
        integrated_input: IntegratedInput,
    ) -> None:
        execution = self._execution_counts.get(self._label, 0) + 1
        self._execution_counts[self._label] = execution
        self._executions.append(self._label)
        if self._label == "b" and execution == 2:
            self._crash_started.set()
            await asyncio.Event().wait()
        ctx.state.count += 1
        target = GraphNode.END if ctx.state.count >= self._terminal_count else self._target
        self.deliver(self._label, target, ctx)


class _PersistentMemoryDeliverStoreFactory(DeliverStoreFactory):
    def __init__(self, stores: list[InMemoryDeliverStore]) -> None:
        self._stores = stores
        self._index = 0

    def create(self) -> DeliverStore:
        if self._index == len(self._stores):
            self._stores.append(InMemoryDeliverStore())
        store = self._stores[self._index]
        self._index += 1
        return store


class _InMemoryCoordinatorFactory(CoordinatorFactory):
    def __init__(self) -> None:
        self._node_stores: dict[int, InMemoryNodeStateStore] = {}
        self._deliver_stores: dict[int, list[InMemoryDeliverStore]] = {}

    def create(
        self,
        graph_instance_id: int,
        instance_store: GraphInstanceStore,
    ) -> GraphPersistenceCoordinator:
        node_store = self._node_stores.setdefault(
            graph_instance_id,
            InMemoryNodeStateStore(graph_instance_id),
        )
        deliver_stores = self._deliver_stores.setdefault(graph_instance_id, [])
        return GraphPersistenceCoordinator(
            graph_instance_id=graph_instance_id,
            instance_store=instance_store,
            node_state_store=node_store,
            default_deliver_store_factory=_PersistentMemoryDeliverStoreFactory(deliver_stores),
        )


async def test_pause_cancel_resume_parallel_sqlite(tmp_path: Path) -> None:
    connection = sqlite3.connect(tmp_path / "pause.db")
    executions: list[str] = []
    branch_a_started = asyncio.Event()
    branch_b_started = asyncio.Event()
    resumed = [False]
    constructors: dict[str, Callable[[], Node[Any]]] = {
        "seed": lambda: _EmitNode("seed", ("branch_a", "branch_b"), executions),
        "branch_a": lambda: _BlockingNode(
            "branch_a", branch_a_started, executions, lambda: not resumed[0]
        ),
        "branch_b": lambda: _BlockingNode(
            "branch_b", branch_b_started, executions, lambda: not resumed[0]
        ),
        "queued": lambda: _EmitNode("queued", (GraphNode.END,), executions),
    }
    orchestrator, instance_store, spec_store = _sqlite_orchestrator(connection, constructors)
    spec_id = spec_store.save(
        GraphSpec(
            name="pause_parallel",
            nodes=[
                _node("seed"),
                _node("branch_a"),
                _node("branch_b"),
                _node("queued", trigger=NodeTrigger.ON_RECEIVE),
            ],
            edges=[
                EdgeSpec(source=GraphNode.START, target="seed"),
                EdgeSpec(source="seed", target="branch_a"),
                EdgeSpec(source="seed", target="branch_b"),
                EdgeSpec(source="seed", target="queued"),
                EdgeSpec(source="branch_a", target=GraphNode.END),
                EdgeSpec(source="branch_b", target=GraphNode.END),
                EdgeSpec(source="queued", target=GraphNode.END),
            ],
            state_class="recovery",
            scheduler=SchedulerKind.PARALLEL,
        )
    )

    execution = asyncio.create_task(orchestrator.create_and_run(spec_id))
    await asyncio.wait_for(
        asyncio.gather(branch_a_started.wait(), branch_b_started.wait()),
        timeout=_TIMEOUT,
    )
    graph_instance_id = _single_instance_id(instance_store, GraphInstanceStatus.RUNNING)

    await orchestrator.deliver_to_node(graph_instance_id, "queued", "external")
    await orchestrator.pause(graph_instance_id)
    assert await asyncio.wait_for(execution, timeout=_TIMEOUT) == graph_instance_id

    assert _status(instance_store, graph_instance_id) == GraphInstanceStatus.PAUSED
    metadata = instance_store.load(graph_instance_id)
    assert metadata is not None
    node_ids = metadata.node_id_map
    coordinator = SqliteCoordinatorFactory(connection).create(graph_instance_id, instance_store)
    for branch in ("branch_a", "branch_b"):
        latest = coordinator.node_state_store.load_latest(node_ids[branch])
        assert latest is not None
        assert latest.status == InvocationStatus.CRASHED
    assert coordinator.node_state_store.load_latest(node_ids["queued"]) is None
    queued_store = coordinator.get_deliver_store(node_ids["queued"])
    assert queued_store is None
    coordinator.register_node(node_ids["queued"])
    queued_store = coordinator.get_deliver_store(node_ids["queued"])
    assert queued_store is not None
    assert [
        record.content
        for record in queued_store.query_consumable(graph_instance_id, node_ids["queued"])
    ] == ["external"]
    assert "queued" not in executions

    resumed[0] = True
    await orchestrator.resume(graph_instance_id)

    assert _status(instance_store, graph_instance_id) == GraphInstanceStatus.COMPLETED
    assert executions.count("seed") == 1
    assert executions.count("branch_a") == 2
    assert executions.count("branch_b") == 2
    assert executions.count("queued") == 1
    recovered = SqliteCoordinatorFactory(connection).create(graph_instance_id, instance_store)
    for branch in ("branch_a", "branch_b"):
        assert [
            record.status
            for record in recovered.node_state_store.query_versions(node_ids[branch])
        ] == [InvocationStatus.COMPLETED, InvocationStatus.CRASHED]
    connection.close()


async def test_stop_is_terminal_and_not_auto_recovered(tmp_path: Path) -> None:
    connection = sqlite3.connect(tmp_path / "stop.db")
    started = asyncio.Event()
    executions: list[str] = []
    constructors: dict[str, Callable[[], Node[Any]]] = {
        "work": lambda: _BlockingNode("work", started, executions, lambda: True),
    }
    orchestrator, instance_store, spec_store = _sqlite_orchestrator(connection, constructors)
    spec_id = spec_store.save(
        GraphSpec(
            name="stop_terminal",
            nodes=[_node("work")],
            edges=[
                EdgeSpec(source=GraphNode.START, target="work"),
                EdgeSpec(source="work", target=GraphNode.END),
            ],
            state_class="recovery",
            scheduler=SchedulerKind.PARALLEL,
        )
    )

    execution = asyncio.create_task(orchestrator.create_and_run(spec_id))
    await asyncio.wait_for(started.wait(), timeout=_TIMEOUT)
    graph_instance_id = _single_instance_id(instance_store, GraphInstanceStatus.RUNNING)
    await orchestrator.stop(graph_instance_id)
    assert await asyncio.wait_for(execution, timeout=_TIMEOUT) == graph_instance_id

    assert _status(instance_store, graph_instance_id) == GraphInstanceStatus.STOPPED
    with pytest.raises(ValueError, match="STOPPED is a terminal status"):
        await orchestrator.resume(graph_instance_id)
    assert await orchestrator.recover_crashed() == []
    assert _status(instance_store, graph_instance_id) == GraphInstanceStatus.STOPPED
    connection.close()


@pytest.mark.parametrize("scheduler", [SchedulerKind.LINEAR, SchedulerKind.PARALLEL])
async def test_process_crash_recovers_without_replaying_completed_prefix(
    tmp_path: Path,
    scheduler: SchedulerKind,
) -> None:
    db_path = tmp_path / f"process-crash-{scheduler.value}.db"
    work_started = asyncio.Event()
    first_executions: list[str] = []
    first_connection = sqlite3.connect(db_path)
    first_orchestrator, first_instance_store, first_spec_store = _sqlite_orchestrator(
        first_connection,
        {
            "prepare": lambda: _EmitNode("prepare", ("work",), first_executions),
            "work": lambda: _BlockingNode(
                "work",
                work_started,
                first_executions,
                lambda: True,
            ),
        },
    )
    spec_id = first_spec_store.save(
        GraphSpec(
            name=f"process_crash_{scheduler.value}",
            nodes=[_node("prepare"), _node("work")],
            edges=[
                EdgeSpec(source=GraphNode.START, target="prepare"),
                EdgeSpec(source="prepare", target="work"),
                EdgeSpec(source="work", target=GraphNode.END),
            ],
            state_class="recovery",
            scheduler=scheduler,
        )
    )

    execution = asyncio.create_task(first_orchestrator.create_and_run(spec_id))
    await asyncio.wait_for(work_started.wait(), timeout=_TIMEOUT)
    graph_instance_id = _single_instance_id(first_instance_store, GraphInstanceStatus.RUNNING)
    execution.cancel()
    with pytest.raises(asyncio.CancelledError):
        await execution
    assert _status(first_instance_store, graph_instance_id) == GraphInstanceStatus.RUNNING
    first_connection.close()

    recovered_executions: list[str] = []
    recovered_connection = sqlite3.connect(db_path)
    recovered_orchestrator, recovered_instance_store, _ = _sqlite_orchestrator(
        recovered_connection,
        {
            "prepare": lambda: _EmitNode("prepare", ("work",), recovered_executions),
            "work": lambda: _EmitNode("work", (GraphNode.END,), recovered_executions),
        },
    )

    assert await recovered_orchestrator.recover_crashed() == [graph_instance_id]
    assert _status(recovered_instance_store, graph_instance_id) == GraphInstanceStatus.COMPLETED
    assert first_executions == ["prepare", "work"]
    assert recovered_executions == ["work"]
    coordinator = SqliteCoordinatorFactory(recovered_connection).create(
        graph_instance_id, recovered_instance_store
    )
    metadata = recovered_instance_store.load(graph_instance_id)
    assert metadata is not None
    assert [
        record.status
        for record in coordinator.node_state_store.query_versions(metadata.node_id_map["prepare"])
    ] == [InvocationStatus.COMPLETED]
    assert [
        record.status
        for record in coordinator.node_state_store.query_versions(metadata.node_id_map["work"])
    ] == [InvocationStatus.COMPLETED, InvocationStatus.CRASHED]
    recovered_connection.close()


async def test_deliver_overlap_is_at_least_once_without_source_dedup(
    tmp_path: Path,
) -> None:
    connection = sqlite3.connect(tmp_path / "overlap.db")
    fail_once = [True]
    executions: list[str] = []
    target_inputs: list[list[object]] = []
    orchestrator, instance_store, spec_store = _sqlite_orchestrator(
        connection,
        {
            "a": lambda: _FailAfterSubmitNode("a", "b", executions, fail_once),
            "b": lambda: _CaptureNode("b", executions, target_inputs),
        },
    )
    spec_id = spec_store.save(
        GraphSpec(
            name="deliver_overlap",
            nodes=[_node("a"), _node("b")],
            edges=[
                EdgeSpec(source=GraphNode.START, target="a"),
                EdgeSpec(source="a", target="b"),
                EdgeSpec(source="b", target=GraphNode.END),
            ],
            state_class="recovery",
        )
    )

    with pytest.raises(RuntimeError, match="source crashed after submit"):
        await orchestrator.create_and_run(spec_id)
    graph_instance_id = _single_instance_id(instance_store, GraphInstanceStatus.CRASHED)
    metadata = instance_store.load(graph_instance_id)
    assert metadata is not None
    node_b_id = metadata.node_id_map["b"]
    coordinator = SqliteCoordinatorFactory(connection).create(graph_instance_id, instance_store)
    coordinator.register_node(node_b_id)
    deliver_store = coordinator.get_deliver_store(node_b_id)
    assert deliver_store is not None
    assert [
        record.content for record in deliver_store.query_consumable(graph_instance_id, node_b_id)
    ] == ["from-a"]

    assert await orchestrator.recover_crashed() == [graph_instance_id]

    assert _status(instance_store, graph_instance_id) == GraphInstanceStatus.COMPLETED
    assert executions == ["a", "a", "b"]
    assert target_inputs == [["from-a", "from-a"]]
    statuses = connection.execute(
        "SELECT status FROM deliver_states "
        "WHERE graph_instance_id = ? AND node_id = ? ORDER BY deliver_id",
        (graph_instance_id, node_b_id),
    ).fetchall()
    assert statuses == [
        (DeliverConsumptionStatus.CONSUMED_COMPLETED.value,),
        (DeliverConsumptionStatus.CONSUMED_COMPLETED.value,),
    ]
    connection.close()


async def test_ring_recovery_uses_latest_version_chain_head(tmp_path: Path) -> None:
    db_path = tmp_path / "ring.db"
    executions: list[str] = []
    execution_counts: dict[str, int] = {}
    crash_started = asyncio.Event()

    def constructors() -> dict[str, Callable[[], Node[Any]]]:
        return {
            "a": lambda: _RingNode("a", "b", 5, executions, execution_counts, crash_started),
            "b": lambda: _RingNode("b", "a", 5, executions, execution_counts, crash_started),
        }

    first_connection = sqlite3.connect(db_path)
    first_orchestrator, first_instance_store, first_spec_store = _sqlite_orchestrator(
        first_connection, constructors()
    )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        spec_id = first_spec_store.save(
            GraphSpec(
                name="ring_recovery",
                nodes=[_node("a"), _node("b")],
                edges=[
                    EdgeSpec(source=GraphNode.START, target="a"),
                    EdgeSpec(source="a", target="b"),
                    EdgeSpec(source="b", target="a"),
                    EdgeSpec(source="a", target=GraphNode.END),
                    EdgeSpec(source="b", target=GraphNode.END),
                ],
                state_class="recovery",
                max_iterations=10,
            )
        )

    execution = asyncio.create_task(first_orchestrator.create_and_run(spec_id))
    await asyncio.wait_for(crash_started.wait(), timeout=_TIMEOUT)
    graph_instance_id = _single_instance_id(first_instance_store, GraphInstanceStatus.RUNNING)
    execution.cancel()
    with pytest.raises(asyncio.CancelledError):
        await execution
    assert _status(first_instance_store, graph_instance_id) == GraphInstanceStatus.RUNNING
    first_connection.close()

    recovered_connection = sqlite3.connect(db_path)
    recovered_orchestrator, recovered_instance_store, _ = _sqlite_orchestrator(
        recovered_connection, constructors()
    )
    assert await recovered_orchestrator.recover_crashed() == [graph_instance_id]

    assert _status(recovered_instance_store, graph_instance_id) == GraphInstanceStatus.COMPLETED
    assert executions == ["a", "b", "a", "b", "b", "a"]
    coordinator = SqliteCoordinatorFactory(recovered_connection).create(
        graph_instance_id, recovered_instance_store
    )
    metadata = recovered_instance_store.load(graph_instance_id)
    assert metadata is not None
    assert [
        record.status
        for record in coordinator.node_state_store.query_versions(metadata.node_id_map["a"])
    ] == [
        InvocationStatus.COMPLETED,
        InvocationStatus.COMPLETED,
        InvocationStatus.COMPLETED,
    ]
    assert [
        record.status
        for record in coordinator.node_state_store.query_versions(metadata.node_id_map["b"])
    ] == [
        InvocationStatus.COMPLETED,
        InvocationStatus.CRASHED,
        InvocationStatus.COMPLETED,
    ]
    assert coordinator.rebuild_main_state()["count"] == 5
    recovered_connection.close()


async def test_null_persistence_fails_safe_and_fresh_start_runs() -> None:
    executions: list[str] = []
    spec_store = InMemoryGraphSpecStore()
    instance_store = NullGraphInstanceStore()
    coordinator_factory = NullCoordinatorFactory()
    orchestrator = GraphOrchestrator(
        node_registry=_registry(
            {"entry": lambda: _EmitNode("entry", (GraphNode.END,), executions)}
        ),
        state_classes={"recovery": RecoveryState},
        spec_store=spec_store,
        instance_store=instance_store,
        coordinator_factory=coordinator_factory,
    )
    spec_id = spec_store.save(
        GraphSpec(
            name="null_fresh_start",
            nodes=[_node("entry")],
            edges=[
                EdgeSpec(source=GraphNode.START, target="entry"),
                EdgeSpec(source="entry", target=GraphNode.END),
            ],
            state_class="recovery",
        )
    )

    graph_instance_id = await orchestrator.create_and_run(spec_id)
    assert executions == ["entry"]
    assert await orchestrator.recover_crashed() == []
    with pytest.raises(ValueError, match="not found"):
        await orchestrator.resume(graph_instance_id)

    coordinator = coordinator_factory.create(graph_instance_id, instance_store)
    coordinator.register_node("entry")
    assert coordinator.node_state_store.load_latest("entry") is None
    assert coordinator.rebuild_main_state() == {}


async def test_in_memory_recovery_has_expected_two_state_degradation() -> None:
    executions: list[str] = []
    worker_inputs: list[list[object]] = []
    crash_once = [True]
    instance_store = InMemoryGraphInstanceStore()
    spec_store = InMemoryGraphSpecStore()
    coordinator_factory = _InMemoryCoordinatorFactory()
    orchestrator = GraphOrchestrator(
        node_registry=_registry(
            {
                "source": lambda: _EmitNode("source", ("worker",), executions, payload="payload"),
                "worker": lambda: _CaptureNode(
                    "worker", executions, worker_inputs, crash_once=crash_once
                ),
            }
        ),
        state_classes={"recovery": RecoveryState},
        spec_store=spec_store,
        instance_store=instance_store,
        coordinator_factory=coordinator_factory,
    )
    spec_id = spec_store.save(
        GraphSpec(
            name="memory_degradation",
            nodes=[_node("source"), _node("worker")],
            edges=[
                EdgeSpec(source=GraphNode.START, target="source"),
                EdgeSpec(source="source", target="worker"),
                EdgeSpec(source="worker", target=GraphNode.END),
            ],
            state_class="recovery",
        )
    )

    with pytest.raises(RuntimeError, match="worker crashed"):
        await orchestrator.create_and_run(spec_id)
    graph_instance_id = _single_instance_id(instance_store, GraphInstanceStatus.CRASHED)
    # CRASHED instance is evicted from _active_instances — access the
    # shared deliver stores via a recovery coordinator that re-registers
    # all nodes in the original order (START, END, source, worker).
    metadata = instance_store.load(graph_instance_id)
    assert metadata is not None
    node_ids = metadata.node_id_map
    recovery_coord = coordinator_factory.create(graph_instance_id, instance_store)
    for node_id in node_ids.values():
        recovery_coord.register_node(node_id)
    worker_store = cast(
        InMemoryDeliverStore,
        recovery_coord.get_deliver_store(node_ids["worker"]),
    )
    records = worker_store._records[graph_instance_id]  # noqa: SLF001
    assert [record.status for record in records] == [DeliverConsumptionStatus.CONSUMED]
    assert worker_store.query_consumable(graph_instance_id, node_ids["worker"]) == []

    assert await orchestrator.recover_crashed() == [graph_instance_id]

    assert _status(instance_store, graph_instance_id) == GraphInstanceStatus.COMPLETED
    assert executions == ["source", "worker", "worker"]
    assert worker_inputs == [["payload"], []]
    assert all(record.status != DeliverConsumptionStatus.CONSUMED_PENDING for record in records)
    coordinator = coordinator_factory.create(graph_instance_id, instance_store)
    assert [
        record.status
        for record in coordinator.node_state_store.query_versions(node_ids["source"])
    ] == [InvocationStatus.COMPLETED]
    assert [
        record.status
        for record in coordinator.node_state_store.query_versions(node_ids["worker"])
    ] == [InvocationStatus.COMPLETED, InvocationStatus.CRASHED]
