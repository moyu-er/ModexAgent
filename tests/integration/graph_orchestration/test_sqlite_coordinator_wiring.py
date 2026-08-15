# ruff: noqa: ANN401, S101

"""SQLite coordinator production wiring and process-restart recovery."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from modex_agent.orchestration import GraphOrchestrator, SqliteCoordinatorFactory
from modex_graph import (
    EdgeSpec,
    FunctionNodeFactory,
    GraphContext,
    GraphInstanceStatus,
    GraphNode,
    GraphSpec,
    GraphState,
    InvocationStatus,
    NodeRegistry,
    NodeSpec,
    SqliteGraphInstanceStore,
    SqliteGraphSpecStore,
)

pytestmark = pytest.mark.integration


class RecoveryState(GraphState):
    steps: list[str] = []


def _spec() -> GraphSpec:
    return GraphSpec(
        name="sqlite_restart_recovery",
        nodes=[
            NodeSpec(name="prepare", node_type="function", config={"function": "prepare"}),
            NodeSpec(name="work", node_type="function", config={"function": "work"}),
        ],
        edges=[
            EdgeSpec(source=GraphNode.START, target="prepare"),
            EdgeSpec(source="prepare", target="work"),
            EdgeSpec(source="work", target=GraphNode.END),
        ],
        state_class="recovery",
    )


def _registry(
    prepare: Any,
    work: Any,
) -> NodeRegistry:
    registry = NodeRegistry()
    registry.register(
        "function",
        FunctionNodeFactory({"prepare": prepare, "work": work}),
    )
    return registry


def _orchestrator(
    connection: sqlite3.Connection,
    node_registry: NodeRegistry,
) -> tuple[GraphOrchestrator, SqliteGraphInstanceStore, SqliteGraphSpecStore]:
    instance_store = SqliteGraphInstanceStore(connection)
    spec_store = SqliteGraphSpecStore(connection)
    orchestrator = GraphOrchestrator(
        node_registry=node_registry,
        state_classes={"recovery": RecoveryState},
        spec_store=spec_store,
        instance_store=instance_store,
        coordinator_factory=SqliteCoordinatorFactory(connection),
    )
    return orchestrator, instance_store, spec_store


async def test_sqlite_restart_recovers_from_crashed_node(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    work_started = asyncio.Event()

    def prepare(ctx: GraphContext[Any]) -> None:
        ctx.state.steps.append("prepare")

    async def blocked_work(ctx: GraphContext[Any]) -> None:
        work_started.set()
        await asyncio.Event().wait()

    first_connection = sqlite3.connect(db_path)
    first_orchestrator, first_instance_store, first_spec_store = _orchestrator(
        first_connection,
        _registry(prepare, blocked_work),
    )
    spec_id = first_spec_store.save(_spec())

    execution = asyncio.create_task(first_orchestrator.create_and_run(spec_id))
    await asyncio.wait_for(work_started.wait(), timeout=2)
    running = first_instance_store.load_by_status(GraphInstanceStatus.RUNNING)
    assert len(running) == 1
    graph_instance_id = running[0].graph_instance_id

    execution.cancel()
    with pytest.raises(asyncio.CancelledError):
        await execution
    first_connection.close()

    replayed_prepare = False

    def unexpected_prepare(ctx: GraphContext[Any]) -> None:
        nonlocal replayed_prepare
        replayed_prepare = True

    recovered_work_initial_steps: list[list[str]] = []

    def recovered_work(ctx: GraphContext[Any]) -> None:
        recovered_work_initial_steps.append(list(ctx.state.steps))
        ctx.state.steps.append("work")

    recovered_connection = sqlite3.connect(db_path)
    try:
        recovered_orchestrator, recovered_instance_store, _ = _orchestrator(
            recovered_connection,
            _registry(unexpected_prepare, recovered_work),
        )

        recovered_ids = await recovered_orchestrator.recover_crashed()

        assert recovered_ids == [graph_instance_id]
        metadata = recovered_instance_store.load(graph_instance_id)
        assert metadata is not None
        assert metadata.status == GraphInstanceStatus.COMPLETED
        assert replayed_prepare is False

        coordinator = SqliteCoordinatorFactory(recovered_connection).create(
            graph_instance_id,
            recovered_instance_store,
        )
        prepare_versions = coordinator.node_state_store.query_versions(
            metadata.node_id_map["prepare"]
        )
        work_versions = coordinator.node_state_store.query_versions(metadata.node_id_map["work"])
        assert [record.status for record in prepare_versions] == [InvocationStatus.COMPLETED]
        assert [record.status for record in work_versions] == [
            InvocationStatus.COMPLETED,
            InvocationStatus.CRASHED,
        ]
        assert recovered_work_initial_steps == [[]]
        assert [record.version for record in work_versions] == [1, 0]
        assert work_versions[0].parent_version is None
    finally:
        recovered_connection.close()
