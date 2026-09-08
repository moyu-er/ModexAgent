"""Recovery selection and failure isolation through the real orchestrator seam."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator

import pytest

from modex_agent.control.graph_recovery import GraphRecoveryService
from modex_agent.orchestration import GraphOrchestrator, SqliteCoordinatorFactory
from modex_graph import (
    DefaultGraphState,
    EdgeSpec,
    FunctionNodeFactory,
    GraphContext,
    GraphInterrupt,
    GraphNode,
    GraphPayload,
    GraphSpec,
    NodeRegistry,
    NodeSpec,
    SqliteGraphInstanceStore,
    SqliteGraphSpecStore,
)
from modex_graph import (
    GraphInstanceStatus as Status,
)


class RecoveryGraph:
    def __init__(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.store = SqliteGraphInstanceStore(self.conn)
        self.specs = SqliteGraphSpecStore(self.conn)
        self.calls: list[int] = []
        self.failures: set[int] = set()
        self.interruptions: set[int] = set()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()
        registry = NodeRegistry()
        registry.register("function", FunctionNodeFactory({"work": self.work}))
        self.orch = GraphOrchestrator(
            node_registry=registry,
            state_classes={"default": DefaultGraphState},
            spec_store=self.specs,
            instance_store=self.store,
            coordinator_factory=SqliteCoordinatorFactory(self.conn),
        )
        self.service = GraphRecoveryService(self.store, self.orch)
        self.spec_id = self.specs.save(
            GraphSpec(
                name="recover",
                state_class="default",
                nodes=[NodeSpec(name="work", node_type="function", config={"function": "work"})],
                edges=[
                    EdgeSpec(source=GraphNode.START, target="work"),
                    EdgeSpec(source="work", target=GraphNode.END),
                ],
            )
        )

    async def work(self, ctx: GraphContext[DefaultGraphState]) -> GraphPayload:
        gid = ctx.graph_instance_id
        assert gid is not None
        self.calls.append(gid)
        self.entered.set()
        await self.release.wait()
        if gid in self.failures:
            raise RuntimeError("recovery failure")
        if gid in self.interruptions:
            ctx.interrupt("approval")
        return GraphPayload(content="recovered")

    async def create(self, status: Status) -> int:
        gid = await self.orch.create_instance(self.spec_id)
        self.store.update_status(gid, status)
        self.orch.unregister_instance(gid)
        return gid


@pytest.fixture
async def graph() -> AsyncIterator[RecoveryGraph]:
    graph = RecoveryGraph()
    try:
        yield graph
    finally:
        graph.release.set()
        await graph.orch.cleanup()
        graph.conn.close()


async def test_auto_recovery_selects_only_explicit_crashed_in_store_order(
    graph: RecoveryGraph,
) -> None:
    ids = {status: await graph.create(status) for status in Status}
    second_crash = await graph.create(Status.CRASHED)
    untouched = {
        gid: graph.store.load(gid) for status, gid in ids.items() if status is not Status.CRASHED
    }
    assert await graph.service.recover_crashed() == [ids[Status.CRASHED], second_crash]
    assert graph.calls == [ids[Status.CRASHED], second_crash]
    for gid, metadata in untouched.items():
        assert graph.store.load(gid) == metadata
    assert await graph.service.recover_crashed() == []


async def test_auto_failure_is_finalized_and_other_candidates_continue(
    graph: RecoveryGraph,
) -> None:
    failed = await graph.create(Status.CRASHED)
    good = await graph.create(Status.CRASHED)
    graph.failures.add(failed)
    assert await graph.service.recover_crashed() == [good]
    assert graph.orch.get_state(failed).metadata.status is Status.CRASHED
    assert graph.orch.get_state(good).metadata.status is Status.COMPLETED
    assert graph.calls == [failed, good]


async def test_manual_recovery_waits_for_real_graph_execution(graph: RecoveryGraph) -> None:
    gid = await graph.create(Status.PAUSED)
    graph.release.clear()
    resume = asyncio.create_task(graph.service.resume(gid))
    await asyncio.wait_for(graph.entered.wait(), 2)
    assert not resume.done()
    assert graph.orch.get_state(gid).metadata.status is Status.RUNNING
    graph.release.set()
    await resume
    assert graph.orch.get_state(gid).metadata.status is Status.COMPLETED


@pytest.mark.parametrize("status", [s for s in Status if s is not Status.PAUSED])
async def test_manual_resume_status_matrix_does_not_mutate_rejected_instances(
    graph: RecoveryGraph,
    status: Status,
) -> None:
    gid = await graph.create(status)
    before = graph.store.load(gid)
    with pytest.raises(ValueError, match="only PAUSED"):
        await graph.service.resume(gid)
    assert graph.store.load(gid) == before
    assert graph.calls == []


async def test_unknown_resume_rejected(graph: RecoveryGraph) -> None:
    with pytest.raises(ValueError, match="not found"):
        await graph.service.resume(999999)


@pytest.mark.parametrize("setup_failure", [False, True])
async def test_manual_failure_propagates_and_finalizes_crashed(
    graph: RecoveryGraph, setup_failure: bool
) -> None:
    gid = await graph.create(Status.PAUSED)
    if setup_failure:
        graph.specs.delete(graph.spec_id)
    else:
        graph.failures.add(gid)
    with pytest.raises((ValueError, RuntimeError)):
        await graph.service.resume(gid)
    assert graph.orch.get_state(gid).metadata.status is Status.CRASHED
    assert graph.orch.get_graph_context(gid) is None


@pytest.mark.parametrize("manual", [False, True])
async def test_recovery_interrupt_is_not_swallowed(graph: RecoveryGraph, manual: bool) -> None:
    gid = await graph.create(Status.PAUSED if manual else Status.CRASHED)
    graph.interruptions.add(gid)
    with pytest.raises(GraphInterrupt):
        if manual:
            await graph.service.resume(gid)
        else:
            await graph.service.recover_crashed()
    assert graph.orch.get_state(gid).metadata.status is Status.PAUSED
