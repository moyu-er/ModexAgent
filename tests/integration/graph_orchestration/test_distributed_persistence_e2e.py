# ruff: noqa: ANN401, S101

"""E2E integration tests for distributed persistence.

Verifies end-to-end scenarios for recovery flow, Node.run lifecycle,
instance pause, and self-loop scheduling. Each scenario exercises real
wiring across multiple components (coordinator + GraphInstance +
GraphOrchestrator + GraphControlService).
"""

from __future__ import annotations

import atexit
import shutil
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from modex_agent.orchestration import GraphOrchestrator, SqliteCoordinatorFactory
from modex_graph import (
    DeliverConsumptionStatus,
    EdgeSpec,
    FunctionNodeFactory,
    GraphContext,
    GraphInstance,
    GraphInstanceStatus,
    GraphInterrupt,
    GraphMetadata,
    GraphNode,
    GraphPersistenceCoordinator,
    GraphSpec,
    GraphState,
    InMemoryDeliverStoreFactory,
    InMemoryGraphInstanceStore,
    InMemoryGraphSpecStore,
    InMemoryNodeStateStore,
    IntegratedInput,
    InvocationContext,
    InvocationStatus,
    Node,
    NodeFactory,
    NodeRegistry,
    NodeSpec,
    RoutingError,
    SqliteGraphInstanceStore,
)

pytestmark = pytest.mark.integration

GID = 500


# ── Shared test state + node fixtures ─────────────────────────────────────


class CounterState(GraphState):
    """Simple state with a counter for testing."""

    count: int = 0


def _increment(ctx: GraphContext[Any]) -> None:
    ctx.state.count += 1


class _InterruptNode(Node[CounterState]):
    """Node that calls ctx.interrupt() to suspend the graph."""

    async def execute(
        self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput
    ) -> None:
        ctx.state.count += 1
        ctx.interrupt("approval_needed")
        return None  # unreachable


class _InterruptFactory(NodeFactory):
    """Factory that creates _InterruptNode instances."""

    def create(self, spec: NodeSpec) -> Node[Any]:
        return _InterruptNode()

    def config_schema(self) -> type[BaseModel] | None:
        return None


# ── Orchestrator + spec builders ──────────────────────────────────────────


def _function_registry() -> NodeRegistry:
    registry = NodeRegistry()
    factory = FunctionNodeFactory({"increment": _increment})
    registry.register("function", factory)
    return registry


def _interrupt_registry() -> NodeRegistry:
    registry = NodeRegistry()
    registry.register("interrupt", _InterruptFactory())
    return registry


def _simple_spec(
    *,
    state_class: str = "counter",
    name: str = "e2e_graph",
) -> GraphSpec:
    return GraphSpec(
        name=name,
        nodes=[
            NodeSpec(name="entry", node_type="function", config={"function": "increment"}),
        ],
        edges=[
            EdgeSpec(source=GraphNode.START, target="entry"),
            EdgeSpec(source="entry", target=GraphNode.END),
        ],
        state_class=state_class,
    )


def _interrupt_spec() -> GraphSpec:
    return GraphSpec(
        name="interrupt_graph",
        nodes=[NodeSpec(name="entry", node_type="interrupt")],
        edges=[
            EdgeSpec(source=GraphNode.START, target="entry"),
            EdgeSpec(source="entry", target=GraphNode.END),
        ],
        state_class="counter",
    )


def _make_orchestrator(
    *,
    node_registry: NodeRegistry | None = None,
) -> tuple[GraphOrchestrator, InMemoryGraphSpecStore, InMemoryGraphInstanceStore]:
    """Build an orchestrator with in-memory stores + test registries."""
    spec_store = InMemoryGraphSpecStore()
    instance_store = InMemoryGraphInstanceStore()
    orchestrator = GraphOrchestrator(
        node_registry=node_registry if node_registry is not None else _function_registry(),
        state_classes={"counter": CounterState},
        spec_store=spec_store,
        instance_store=instance_store,
    )
    return orchestrator, spec_store, instance_store


def _save_spec(spec_store: InMemoryGraphSpecStore, spec: GraphSpec) -> int:
    return spec_store.save(spec)


def _load_status(store: InMemoryGraphInstanceStore, gid: int) -> str:
    instance = store.load(gid)
    assert instance is not None, f"Instance {gid} not found in store"
    return instance.status


def _get_coordinator(orch: GraphOrchestrator, gid: int) -> GraphPersistenceCoordinator:
    instance = orch._active_instances.get(gid)
    assert instance is not None, f"Instance {gid} not in _active_instances"
    return instance.coordinator


# ── Coordinator builders (Memory / SQLite) ────────────────────────────────


def _metadata(
    gid: int = GID,
    status: GraphInstanceStatus = GraphInstanceStatus.RUNNING,
) -> GraphMetadata:
    return GraphMetadata(
        graph_instance_id=gid,
        spec_id=202,
        parent_instance_id=None,
        parent_node=None,
        status=status,
    )


def _memory_coordinator(gid: int = GID) -> GraphPersistenceCoordinator:
    """Memory-strategy coordinator for suspend/resume tests."""
    return GraphPersistenceCoordinator(
        graph_instance_id=gid,
        instance_store=InMemoryGraphInstanceStore(),
        node_state_store=InMemoryNodeStateStore(gid),
        default_deliver_store_factory=InMemoryDeliverStoreFactory(),
    )


def _sqlite_coordinator(
    conn: sqlite3.Connection | None = None,
    gid: int = GID,
    db_path: str | None = None,
) -> tuple[GraphPersistenceCoordinator, sqlite3.Connection, str]:
    """SQLite-strategy coordinator for crash-recovery tests.

    Uses a shared temp file so coord2 (constructed with the same db_path)
    can see coord's instance_store writes. ``:memory:`` DBs are
    per-connection and would hide coord's metadata from coord2.
    """
    import tempfile

    if db_path is None:
        tmp_dir = tempfile.mkdtemp(prefix="modex_e2e_")
        db_path = str(Path(tmp_dir) / "instances.db")
        atexit.register(_cleanup_db_dir, tmp_dir)
    conn = conn or sqlite3.connect(db_path)
    coord = SqliteCoordinatorFactory(conn).create(
        gid,
        SqliteGraphInstanceStore(conn),
    )
    return coord, conn, db_path


def _cleanup_db_dir(tmp_dir: str) -> None:
    shutil.rmtree(tmp_dir, ignore_errors=True)


def _simulate_node_run_complete(
    coord: GraphPersistenceCoordinator,
    node_name: str,
) -> InvocationContext:
    """Simulate the Node.run() lifecycle: begin -> complete.

    Returns the InvocationContext so callers can inspect it.
    """
    inv = coord.node_state_store.begin_invocation(node_name)
    coord.node_state_store.complete_invocation(inv)
    return inv


def _simulate_node_run_crash(
    coord: GraphPersistenceCoordinator,
    node_name: str,
) -> InvocationContext:
    """Simulate the Node.run() lifecycle: begin -> crash.

    Returns the InvocationContext so callers can inspect it.
    """
    inv = coord.node_state_store.begin_invocation(node_name)
    coord.node_state_store.crash_invocation(inv)
    return inv


# ══════════════════════════════════════════════════════════════════════════
# Scenario 1: Normal execution — GraphOrchestrator create_and_run -> COMPLETED
# ══════════════════════════════════════════════════════════════════════════


class TestScenario1NormalExecution:
    """Normal path: GraphOrchestrator create_and_run -> completion.

    Verifies the full wiring: GraphSpec -> compile -> GraphInstance ->
    GraphEngine -> scheduler -> Node.run() -> coordinator lifecycle ->
    COMPLETED status.
    """

    async def test_normal_execution_completes(self) -> None:
        orch, spec_store, instance_store = _make_orchestrator()
        spec_id = _save_spec(spec_store, _simple_spec())

        gid = await orch.create_and_run(spec_id)

        # GraphInstance status = COMPLETED (persisted in instance_store).
        assert _load_status(instance_store, gid) == GraphInstanceStatus.COMPLETED.value
        # COMPLETED instances are evicted from _active_instances (M1).
        assert gid not in orch._active_instances
        # State queries still work via the store for evicted instances.
        state = orch.get_state(gid)
        assert state.metadata.graph_instance_id == gid

    async def test_state_mutated_by_function_node(self) -> None:
        """The function node mutates state through the full execution chain."""
        orch, spec_store, _ = _make_orchestrator()
        spec_id = _save_spec(spec_store, _simple_spec())

        state = CounterState(count=0)
        await orch.create_and_run(spec_id, initial_state=state)

        assert state.count == 1


# ══════════════════════════════════════════════════════════════════════════
# Scenario 2: Retired node-level snapshot model
# ══════════════════════════════════════════════════════════════════════════

# Node-level snapshot/suspension tests were retired. GraphInterrupt cancellation
# and four-state deliver crash windows are covered by test_crash_window_matrix.py.


# ══════════════════════════════════════════════════════════════════════════
# Scenario 3: Crash recovery (SQLite coordinator)
# ══════════════════════════════════════════════════════════════════════════


class TestScenario3CrashRecoverySqlite:
    """Recovery flow: execute -> crash -> restart -> verify lifecycle facts.

    SQLite strategy provides crash recovery (data persists across process
    restarts). A new coordinator over the same database identifies the
    CRASHED node for re-dispatch through its invocation record.
    """

    async def test_crash_recovery_sqlite_coordinator(self) -> None:
        coord, conn, db_path = _sqlite_coordinator()
        coord.register_node("worker")
        coord._instance_store.save(_metadata())

        # Simulate: execute -> crash.
        _simulate_node_run_crash(coord, "worker")

        # Verify: coordinator has CRASHED invocation.
        latest = coord.node_state_store.load_latest("worker")
        assert latest is not None
        assert latest.status == InvocationStatus.CRASHED

        # Simulate process restart: new coordinator with same connection.
        coord2, _, _ = _sqlite_coordinator(conn=conn, db_path=db_path)
        coord2.register_node("worker")

        # The CRASHED node is identified for re-dispatch.
        node_state = coord2.node_state_store.load_latest("worker")
        assert node_state is not None
        assert node_state.status == InvocationStatus.CRASHED

        # Re-dispatch: new coordinator creates a new invocation.
        inv_new = coord2.node_state_store.begin_invocation("worker")
        assert inv_new.version == 1  # v0 was CRASHED, v1 is new
        assert inv_new.parent_version is None  # no prior COMPLETED

        # Complete the re-dispatched invocation.
        coord2.node_state_store.complete_invocation(inv_new)

        worker_state = coord2.node_state_store.load_latest("worker")
        assert worker_state is not None
        assert worker_state.status == InvocationStatus.COMPLETED

    async def test_crash_after_partial_completion(self) -> None:
        """Crash after one node COMPLETED, before another finishes.

        Recovery should rebuild main_state from the COMPLETED node and
        identify the CRASHED node for re-dispatch.
        """
        coord, conn, db_path = _sqlite_coordinator()
        coord.register_node("node_a")
        coord.register_node("node_b")
        coord._instance_store.save(_metadata())

        # node_a completes, node_b crashes.
        _simulate_node_run_complete(coord, "node_a")
        _simulate_node_run_crash(coord, "node_b")

        # New coordinator (process restart).
        coord2, _, _ = _sqlite_coordinator(conn=conn, db_path=db_path)
        coord2.register_node("node_a")
        coord2.register_node("node_b")

        # node_a is COMPLETED (skip on re-dispatch).
        node_a_state = coord2.node_state_store.load_latest("node_a")
        assert node_a_state is not None
        assert node_a_state.status == InvocationStatus.COMPLETED
        # node_b is CRASHED (re-dispatch).
        node_b_state = coord2.node_state_store.load_latest("node_b")
        assert node_b_state is not None
        assert node_b_state.status == InvocationStatus.CRASHED


# ══════════════════════════════════════════════════════════════════════════
# Scenario 5: crash-between (save COMPLETED before promote)
# ══════════════════════════════════════════════════════════════════════════


class TestScenario5F2CrashBetween:
    """Crash between save COMPLETED and promote_delivers.

    Simulate: begin -> route_deliver -> mark_consumed -> save COMPLETED
    (skip promote_delivers) -> crash. Recovery: promote_delivers ->
    auto-promote CONSUMED_PENDING -> CONSUMED_COMPLETED.
    """

    async def test_f2_crash_after_complete_before_promote(self) -> None:
        coord, conn, db_path = _sqlite_coordinator()
        coord.register_node("worker")
        coord._instance_store.save(_metadata())

        inv = coord.node_state_store.begin_invocation("worker")

        # Deliver + mark consumed.
        d1 = coord.route_deliver("worker", {"data": 1}, "source", 9999)
        d2 = coord.route_deliver("worker", {"data": 2}, "source", 9999)
        assert d1 is not None and d2 is not None

        store = coord.get_deliver_store("worker")
        assert store is not None
        store.mark_consumed([d1, d2], inv.invocation_id)

        # Simulate crash: save COMPLETED directly (skip promote_delivers).
        coord.node_state_store.complete_invocation(inv)

        # Verify: delivers are CONSUMED_PENDING before recovery.
        consumable_before = store.query_consumable(GID, "worker")
        consumed_pending = [
            r for r in consumable_before if r.status == DeliverConsumptionStatus.CONSUMED_PENDING
        ]
        assert len(consumed_pending) == 2

        # New coordinator (process restart).
        coord2, _, _ = _sqlite_coordinator(conn=conn, db_path=db_path)
        coord2.register_node("worker")

        # Recovery: auto-promote CONSUMED_PENDING -> CONSUMED_COMPLETED.
        coord2.promote_delivers("worker", inv.invocation_id)

        # Verify: no CONSUMED_PENDING remains.
        consumable_after = store.query_consumable(GID, "worker")
        for r in consumable_after:
            assert r.status != DeliverConsumptionStatus.CONSUMED_PENDING

        # Verify: delivers are now CONSUMED_COMPLETED.
        rows = conn.execute(
            "SELECT status FROM deliver_states WHERE graph_instance_id = ? AND node_id = ?",
            (GID, "worker"),
        ).fetchall()
        statuses = [row[0] for row in rows]
        completed_count = sum(
            1 for s in statuses if s == DeliverConsumptionStatus.CONSUMED_COMPLETED.value
        )
        assert completed_count == 2


# ══════════════════════════════════════════════════════════════════════════
# Scenario 6: Self-loop (A -> A) scheduling verification
# ══════════════════════════════════════════════════════════════════════════


class TestScenario6SelfLoop:
    """Self-loop node (A -> A) scheduling verification.

    A executes -> delivers to itself -> A completes -> A re-dispatched ->
    A consumes its own deliver. Serial execution: A's first invocation
    COMPLETED before the second begins (coordinator version chain enforces
    this — each begin_invocation creates a new version).
    """

    async def test_self_loop_a_to_a(self) -> None:
        coord = _memory_coordinator()
        coord.register_node("node_a")
        coord._instance_store.save(_metadata())

        # v0: A executes and delivers to itself.
        inv0 = coord.node_state_store.begin_invocation("node_a")
        assert inv0.version == 0

        # A delivers to itself (self-loop).
        d1 = coord.route_deliver(
            target_node_id="node_a",
            content={"step": 1},
            source_node_id="node_a",
            source_invocation_id=inv0.invocation_id,
        )
        assert d1 is not None

        # A completes v0.
        coord.node_state_store.complete_invocation(inv0)
        assert inv0.version == 0

        # v0 is COMPLETED before v1 begins (serial execution).
        latest = coord.node_state_store.load_latest("node_a")
        assert latest is not None
        assert latest.status == InvocationStatus.COMPLETED

        # A re-dispatched: v1 consumes its own deliver from v0.
        inv1 = coord.node_state_store.begin_invocation("node_a")
        assert inv1.version == 1
        assert inv1.parent_version == 0  # parent is v0 (COMPLETED)

        # v1 consumes the self-deliver.
        consumable = coord.collect_consumable_delivers("node_a", inv1.invocation_id)
        assert len(consumable) == 1
        assert consumable[0].content == {"step": 1}
        assert consumable[0].source_node_id == "node_a"

        coord.mark_delivers_consumed(
            "node_a", [r.deliver_id for r in consumable], inv1.invocation_id
        )

        # v1 completes — promotes the consumed deliver.
        coord.node_state_store.complete_invocation(inv1)
        coord.promote_delivers("node_a", inv1.invocation_id)

        # Verify: the deliver is now consumed (not pending).
        store = coord.get_deliver_store("node_a")
        assert store is not None
        remaining = store.query_consumable(GID, "node_a")
        assert len(remaining) == 0

    async def test_self_loop_serial_execution(self) -> None:
        """Serial: A not running in parallel with itself.

        The coordinator's version chain enforces serial execution: each
        begin_invocation creates a new version only after the previous
        invocation reaches a terminal state (or is marked CRASHED).
        """
        coord = _memory_coordinator()
        coord.register_node("node_a")

        # v0 begins.
        inv0 = coord.node_state_store.begin_invocation("node_a")
        assert inv0.version == 0

        # v0 must complete before v1 can begin (serial).
        coord.node_state_store.complete_invocation(inv0)

        # v1 begins after v0 completes.
        inv1 = coord.node_state_store.begin_invocation("node_a")
        assert inv1.version == 1
        assert inv1.parent_version == 0

        coord.node_state_store.complete_invocation(inv1)

        # Version chain: 0 (COMPLETED) -> 1 (COMPLETED).
        versions = coord.node_state_store.query_versions("node_a")
        assert len(versions) == 2
        assert all(v.status == InvocationStatus.COMPLETED for v in versions)


# ══════════════════════════════════════════════════════════════════════════
# Scenario 7: Dual path (ReActAgent + GraphOrchestrator)
# ══════════════════════════════════════════════════════════════════════════


class TestScenario7InstancePause:
    """GraphInterrupt cancellation and graph-instance PAUSED remain separate."""

    async def test_graph_orchestrator_path_suspend_persists(self) -> None:
        """GraphOrchestrator path: GraphInterrupt -> PAUSED status.

        The orchestrator catches GraphInterrupt, sets status to PAUSED,
        and the GraphInstance stays in the registry (coordinator alive).
        """
        orch, spec_store, instance_store = _make_orchestrator(node_registry=_interrupt_registry())
        spec_id = _save_spec(spec_store, _interrupt_spec())

        with pytest.raises(GraphInterrupt):
            await orch.create_and_run(spec_id)

        # Status = PAUSED (persisted in instance_store).
        gids = list(orch._active_instances.keys())
        assert len(gids) == 1
        gid = gids[0]
        assert _load_status(instance_store, gid) == GraphInstanceStatus.PAUSED.value

        # Instance stays in registry (coordinator alive for resume — I1).
        assert gid in orch._active_instances

# ══════════════════════════════════════════════════════════════════════════
# Scenario 8: GraphControlService deliver convergence
# ══════════════════════════════════════════════════════════════════════════


class TestScenario8DeliverConvergence:
    """External DELIVER_TO_NODE -> coordinator.route_deliver.

    External delivers converge on coordinator.route_deliver(source=
    "__external__"). The deliver goes to the per-node deliver_store inside
    the coordinator — no shared deliver_store.
    """

    async def test_deliver_routes_through_coordinator(self) -> None:
        orch, spec_store, _ = _make_orchestrator()
        spec_id = _save_spec(spec_store, _simple_spec())
        gid = await orch.create_instance(spec_id)

        await orch.deliver_to_node(gid, "entry", "hello")

        coordinator = _get_coordinator(orch, gid)
        node_id = orch._active_instances[gid].metadata.node_id_map["entry"]
        store = coordinator.get_deliver_store(node_id)
        assert store is not None
        pending = store.query_consumable(gid, node_id)
        assert len(pending) == 1
        assert pending[0].content == "hello"
        assert pending[0].source_node_id == "__external__"
        assert pending[0].source_invocation_id == 0

    async def test_no_shared_deliver_store(self) -> None:
        """Each node has its own deliver_store (no shared store).

        Delivering to node A does not appear in node B's deliver_store.
        """
        orch, spec_store, _ = _make_orchestrator()

        # Two-node graph.
        spec = GraphSpec(
            name="two_node",
            nodes=[
                NodeSpec(name="a", node_type="function", config={"function": "increment"}),
                NodeSpec(name="b", node_type="function", config={"function": "increment"}),
            ],
            edges=[
                EdgeSpec(source=GraphNode.START, target="a"),
                EdgeSpec(source="a", target="b"),
                EdgeSpec(source="b", target=GraphNode.END),
            ],
            state_class="counter",
        )
        spec_id = _save_spec(spec_store, spec)
        gid = await orch.create_instance(spec_id)

        coordinator = _get_coordinator(orch, gid)
        node_ids = orch._active_instances[gid].metadata.node_id_map
        store_a = coordinator.get_deliver_store(node_ids["a"])
        store_b = coordinator.get_deliver_store(node_ids["b"])
        assert store_a is not None
        assert store_b is not None
        assert store_a is not store_b

        # Deliver to node A.
        await orch.deliver_to_node(gid, "a", "data_for_a")
        assert len(store_a.query_consumable(gid, node_ids["a"])) == 1
        # Node B's store is empty (no shared store).
        assert len(store_b.query_consumable(gid, node_ids["b"])) == 0

    async def test_deliver_to_unknown_instance_raises(self) -> None:
        """Deliver to unknown gid raises ValueError (no active coordinator)."""
        orch, _, _ = _make_orchestrator()

        with pytest.raises(ValueError, match="No active graph instance"):
            await orch.deliver_to_node(888888, "node", "data")

    async def test_deliver_to_unregistered_node_raises(self) -> None:
        """Delivering to a node not in the graph raises RoutingError."""
        orch, spec_store, _ = _make_orchestrator()
        spec_id = _save_spec(spec_store, _simple_spec())
        gid = await orch.create_instance(spec_id)

        with pytest.raises(RoutingError, match="no deliver_store"):
            await orch.deliver_to_node(gid, "nonexistent_node", "data")


# ══════════════════════════════════════════════════════════════════════════
# Scenario 9: GraphInstance eviction
# ══════════════════════════════════════════════════════════════════════════


class TestScenario9GraphInstanceEviction:
    """Terminal -> unregister_instance -> coordinator.close.

    After terminal status, unregister_instance evicts the GraphInstance:
    calls coordinator.close() (a no-op — the coordinator owns no
    connections; the caller manages the ``sqlite3.Connection`` lifetime)
    and removes from _active_instances. After eviction, _lookup_coordinator
    returns None (external delivers fail).
    """

    async def test_eviction_removes_from_registry(self) -> None:
        """unregister_instance removes from _active_instances."""
        orch, spec_store, _ = _make_orchestrator()
        spec_id = _save_spec(spec_store, _simple_spec())
        gid = await orch.create_instance(spec_id)

        assert gid in orch._active_instances
        orch.unregister_instance(gid)
        assert gid not in orch._active_instances

    async def test_eviction_closes_coordinator(self) -> None:
        """unregister_instance calls coordinator.close()."""
        orch, spec_store, _ = _make_orchestrator()
        spec_id = _save_spec(spec_store, _simple_spec())
        gid = await orch.create_instance(spec_id)

        coordinator = _get_coordinator(orch, gid)
        close_called = False
        original_close = coordinator.close

        def tracking_close() -> None:
            nonlocal close_called
            close_called = True
            original_close()

        coordinator.close = tracking_close  # type: ignore[method-assign]
        orch.unregister_instance(gid)

        assert close_called is True

    async def test_eviction_blocks_subsequent_delivers(self) -> None:
        """After eviction, _lookup_coordinator returns None -> delivers fail."""
        orch, spec_store, _ = _make_orchestrator()
        spec_id = _save_spec(spec_store, _simple_spec())
        gid = await orch.create_and_run(spec_id)

        orch.unregister_instance(gid)

        # _lookup_coordinator returns None after eviction.
        assert orch._lookup_coordinator(gid) is None

        # Subsequent delivers fail.
        with pytest.raises(ValueError, match="No active graph instance"):
            await orch.deliver_to_node(gid, "entry", "data")

    async def test_eviction_unknown_gid_noop(self) -> None:
        """unregister on unknown gid is a safe no-op."""
        orch, _, _ = _make_orchestrator()
        orch.unregister_instance(999999)
        assert 999999 not in orch._active_instances

    async def test_sqlite_coordinator_close_is_noop_on_connection(self) -> None:
        """SQLite coordinator's close() is a no-op on the shared connection.

        The coordinator owns no connections — stores take a caller-owned
        ``sqlite3.Connection`` and never close it. After eviction, the
        shared connection must still be usable (the caller owns its
        lifetime).
        """
        orch, spec_store, instance_store = _make_orchestrator()

        # Create a SQLite coordinator + GraphInstance manually.
        conn = sqlite3.connect(":memory:")
        coord, _, _ = _sqlite_coordinator(conn=conn, gid=GID)
        coord.register_node("worker")
        metadata = _metadata(gid=GID, status=GraphInstanceStatus.COMPLETED)
        instance_store.save(metadata)
        instance = GraphInstance(metadata, coord)

        # Manually register in the orchestrator's _active_instances.
        orch._active_instances[GID] = instance

        orch.unregister_instance(GID)
        assert GID not in orch._active_instances

        assert conn.execute("SELECT 1").fetchone() == (1,)
        conn.close()

    async def test_full_lifecycle_create_complete_evict(self) -> None:
        """Full lifecycle: create -> execute -> complete -> evict."""
        orch, spec_store, instance_store = _make_orchestrator()
        spec_id = _save_spec(spec_store, _simple_spec())

        # Create + execute.
        gid = await orch.create_and_run(spec_id)
        assert _load_status(instance_store, gid) == GraphInstanceStatus.COMPLETED.value
        # COMPLETED -> evicted from _active_instances (M1).
        assert gid not in orch._active_instances
        assert orch._lookup_coordinator(gid) is None
