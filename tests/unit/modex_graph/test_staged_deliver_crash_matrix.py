from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest
from helpers import CounterState, make_runtime

from modex_agent.agents.agent_node import AgentNode
from modex_agent.persistence.session_registry import (
    InMemorySessionRegistry,
    SessionRegistry,
)
from modex_graph import (
    CompiledGraph,
    DeliverConsumptionStatus,
    Graph,
    GraphContext,
    GraphNode,
    GraphPersistenceCoordinator,
    IntegratedInput,
    InvocationContext,
    Node,
    SchedulerKind,
    SqliteDeliverStoreFactory,
    SqliteGraphInstanceStore,
    SqliteNodeStateStore,
)
from modex_graph.scheduler.bootstrap import BootstrapMode, bootstrap


class _FailCompleteOnceSqliteNodeStateStore(SqliteNodeStateStore):
    def __init__(
        self,
        connection: sqlite3.Connection,
        graph_instance_id: int,
        failing_node_id: str,
    ) -> None:
        super().__init__(connection, graph_instance_id)
        self._failing_node_id = failing_node_id
        self._failed = False

    def complete_invocation(self, invocation: InvocationContext) -> None:
        if invocation.node_id == self._failing_node_id and not self._failed:
            self._failed = True
            raise RuntimeError("crashed before complete_invocation")
        super().complete_invocation(invocation)


class _AttemptingSource(Node[CounterState]):
    def __init__(self) -> None:
        self.attempt = 0

    async def execute(
        self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput
    ) -> None:
        self.attempt += 1
        self.deliver(f"attempt-{self.attempt}", "b", ctx)


class _RecordingTarget(Node[CounterState]):
    def __init__(self, *, crash_once: bool = False) -> None:
        self.crash_once = crash_once
        self.inputs: list[IntegratedInput] = []

    async def execute(
        self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput
    ) -> None:
        self.inputs.append(integrated_input)
        if self.crash_once:
            self.crash_once = False
            raise RuntimeError("crashed after input integration")


class _RecordingAgentTarget(AgentNode):
    def __init__(self) -> None:
        super().__init__()
        self.crash_once = True
        self.inputs: list[IntegratedInput] = []

    def agent_name(self) -> str:
        return "crash-matrix-agent"

    async def _resolve_session_registry(self) -> SessionRegistry:
        return InMemorySessionRegistry()

    async def execute(self, ctx: GraphContext[Any], integrated_input: IntegratedInput) -> None:
        self.inputs.append(integrated_input)
        if self.crash_once:
            self.crash_once = False
            raise RuntimeError("crashed after agent input integration")


def _compile_pair(
    source: Node[CounterState], target: Node[CounterState]
) -> CompiledGraph[CounterState]:
    graph: Graph[CounterState] = Graph()
    graph.add_node("a", source)
    graph.add_node("b", target)
    graph.add_edge(GraphNode.START, "a")
    graph.add_edge("a", "b")
    graph.add_edge("b", GraphNode.END)
    return graph.compile()


def _coordinator(
    db_path: Path,
    graph_instance_id: int,
    compiled: CompiledGraph[CounterState],
    *,
    fail_complete_node_id: str | None = None,
) -> tuple[GraphPersistenceCoordinator, sqlite3.Connection]:
    connection = sqlite3.connect(db_path)
    node_state_store = (
        _FailCompleteOnceSqliteNodeStateStore(
            connection,
            graph_instance_id,
            fail_complete_node_id,
        )
        if fail_complete_node_id is not None
        else SqliteNodeStateStore(connection, graph_instance_id)
    )
    coordinator = GraphPersistenceCoordinator(
        graph_instance_id=graph_instance_id,
        instance_store=SqliteGraphInstanceStore(connection),
        node_state_store=node_state_store,
        default_deliver_store_factory=SqliteDeliverStoreFactory(connection),
    )
    for node in compiled.nodes.values():
        coordinator.register_node(node.node_id)
    return coordinator, connection


def _context(
    coordinator: GraphPersistenceCoordinator,
    graph_instance_id: int,
) -> GraphContext[CounterState]:
    ctx = GraphContext(
        state=CounterState(),
        runtime=make_runtime(),
        coordinator=coordinator,
        scheduler_kind=SchedulerKind.LINEAR,
        graph_instance_id=graph_instance_id,
    )
    ctx.set_dispatch_handler(lambda _source, _target: None)
    return ctx


def _deliver_statuses(
    connection: sqlite3.Connection,
    graph_instance_id: int,
    source_node_id: str,
) -> list[str]:
    rows = connection.execute(
        "SELECT status FROM deliver_states "
        "WHERE graph_instance_id = ? AND source_node_id = ? ORDER BY deliver_id",
        (graph_instance_id, source_node_id),
    ).fetchall()
    return [str(row[0]) for row in rows]


class TestWindow1ExecuteCrash:
    async def test_staged_survives_and_retry_promotes_both_outputs(self, tmp_path: Path) -> None:
        graph_instance_id = 9101
        source = _AttemptingSource()
        target = _RecordingTarget()
        compiled = _compile_pair(source, target)
        source_id = compiled.nodes["a"].node_id
        target_id = compiled.nodes["b"].node_id
        db_path = tmp_path / "window-1.db"
        coordinator, connection = _coordinator(
            db_path,
            graph_instance_id,
            compiled,
            fail_complete_node_id=source_id,
        )

        with pytest.raises(RuntimeError, match="before complete_invocation"):
            await source.run(_context(coordinator, graph_instance_id), graph=compiled)
        assert _deliver_statuses(connection, graph_instance_id, source_id) == ["staged"]
        connection.close()

        recovered, connection = _coordinator(db_path, graph_instance_id, compiled)
        recovered_ctx = _context(recovered, graph_instance_id)
        assert bootstrap(recovered_ctx, compiled, mode=BootstrapMode.RECOVERY) == ["a"]
        await source.run(recovered_ctx, graph=compiled)

        pending = recovered.collect_consumable_delivers(target_id, 0)
        assert [record.content for record in pending] == ["attempt-1", "attempt-2"]
        assert len({record.deliver_id for record in pending}) == 2
        assert all(record.status is DeliverConsumptionStatus.PENDING for record in pending)

        await target.run(recovered_ctx, graph=compiled)
        assert target.inputs[-1].integrated_content == ["attempt-1", "attempt-2"]
        assert DeliverConsumptionStatus.STAGED.value not in _deliver_statuses(
            connection, graph_instance_id, source_id
        )
        connection.close()


class TestWindow2CompleteBeforePromoteCrash:
    async def test_bootstrap_promotes_completed_sources_staged_output(
        self, tmp_path: Path
    ) -> None:
        graph_instance_id = 9102
        source = _AttemptingSource()
        target = _RecordingTarget()
        compiled = _compile_pair(source, target)
        source_id = compiled.nodes["a"].node_id
        target_id = compiled.nodes["b"].node_id
        db_path = tmp_path / "window-2.db"
        coordinator, connection = _coordinator(db_path, graph_instance_id, compiled)
        invocation = coordinator.node_state_store.begin_invocation(source_id)
        coordinator.route_deliver(target_id, "stranded", source_id, invocation.invocation_id, stage=True)
        coordinator.node_state_store.complete_invocation(invocation)
        assert _deliver_statuses(connection, graph_instance_id, source_id) == ["staged"]
        connection.close()

        recovered, connection = _coordinator(db_path, graph_instance_id, compiled)
        recovered_ctx = _context(recovered, graph_instance_id)
        assert bootstrap(recovered_ctx, compiled, mode=BootstrapMode.RECOVERY) == ["b"]
        pending = recovered.collect_consumable_delivers(target_id, 0)
        assert [record.content for record in pending] == ["stranded"]
        assert pending[0].status is DeliverConsumptionStatus.PENDING

        await target.run(recovered_ctx, graph=compiled)
        assert target.inputs[-1].integrated_content == ["stranded"]
        connection.close()


class TestWindow3PromoteBeforeDispatchCrash:
    async def test_bootstrap_pending_rescan_seeds_undispatched_target(
        self, tmp_path: Path
    ) -> None:
        graph_instance_id = 9103
        source = _AttemptingSource()
        target = _RecordingTarget()
        compiled = _compile_pair(source, target)
        source_id = compiled.nodes["a"].node_id
        target_id = compiled.nodes["b"].node_id
        db_path = tmp_path / "window-3.db"
        coordinator, connection = _coordinator(db_path, graph_instance_id, compiled)
        invocation = coordinator.node_state_store.begin_invocation(source_id)
        coordinator.route_deliver(target_id, "promoted", source_id, invocation.invocation_id, stage=True)
        coordinator.node_state_store.complete_invocation(invocation)
        assert coordinator.promote_staged_by_source(graph_instance_id, source_id) == {target_id}
        connection.close()

        recovered, connection = _coordinator(db_path, graph_instance_id, compiled)
        recovered_ctx = _context(recovered, graph_instance_id)
        assert bootstrap(recovered_ctx, compiled, mode=BootstrapMode.RECOVERY) == ["b"]
        await target.run(recovered_ctx, graph=compiled)
        assert target.inputs[-1].integrated_content == ["promoted"]
        connection.close()


class TestWindow4ConsumedBeforeCompleteCrash:
    async def test_base_node_reintegrates_consumed_pending_input(self, tmp_path: Path) -> None:
        graph_instance_id = 9104
        target = _RecordingTarget(crash_once=True)
        compiled = _compile_pair(_AttemptingSource(), target)
        target_id = compiled.nodes["b"].node_id
        db_path = tmp_path / "window-4-base.db"
        coordinator, connection = _coordinator(db_path, graph_instance_id, compiled)
        coordinator.route_deliver(target_id, "prior-consumed", "external", 0)

        with pytest.raises(RuntimeError, match="after input integration"):
            await target.run(_context(coordinator, graph_instance_id), graph=compiled)
        consumed_pending = coordinator.collect_consumable_delivers(target_id, 0)
        assert consumed_pending[0].status is DeliverConsumptionStatus.CONSUMED_PENDING
        connection.close()

        recovered, connection = _coordinator(db_path, graph_instance_id, compiled)
        await target.run(_context(recovered, graph_instance_id), graph=compiled)
        assert target.inputs[-1].integrated_content == ["prior-consumed"]
        assert target.inputs[-1].payloads[0].status is DeliverConsumptionStatus.CONSUMED_PENDING
        connection.close()

    async def test_agent_node_filters_consumed_pending_input(self, tmp_path: Path) -> None:
        graph_instance_id = 9105
        target = _RecordingAgentTarget()
        compiled = _compile_pair(_AttemptingSource(), target)
        target_id = compiled.nodes["b"].node_id
        db_path = tmp_path / "window-4-agent.db"
        coordinator, connection = _coordinator(db_path, graph_instance_id, compiled)
        coordinator.route_deliver(target_id, "session-input", "external", 0)

        with pytest.raises(RuntimeError, match="after agent input integration"):
            await target.run(_context(coordinator, graph_instance_id), graph=compiled)
        assert target.inputs[-1].integrated_content == ["session-input"]
        connection.close()

        recovered, connection = _coordinator(db_path, graph_instance_id, compiled)
        await target.run(_context(recovered, graph_instance_id), graph=compiled)

        assert target.inputs[-1].integrated_content == []
        assert target.inputs[-1].payloads == []
        connection.close()
