# ruff: noqa: ANN401, S101

"""Tests for ``GraphPersistenceCoordinator``.

Covers:

- Constructor takes ``node_state_store`` (not ``default_node_state_factory``).
- register_node uses default deliver factory.
- route_deliver skips END.
- Lifecycle methods are on ``node_state_store``, not coordinator.
- promote_delivers upgrades ALL CONSUMED_PENDING for node.
- rebuild_main_state picks single newest snapshot per node.
- Crash between save COMPLETED + promote → recovery → auto-promote.
- close() is a safe-to-call no-op (coordinator owns no connections).
- Null strategy via create_null_coordinator.
- ``CoordinatorFactory`` ABC + ``NullCoordinatorFactory`` default.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from modex_graph import (
    CoordinatorFactory,
    DeliverConsumptionStatus,
    GraphInstanceStatus,
    GraphMetadata,
    GraphPersistenceCoordinator,
    GraphStateSnapshot,
    InMemoryDeliverStore,
    InMemoryDeliverStoreFactory,
    InMemoryGraphInstanceStore,
    InMemoryNodeStateStore,
    InvocationContext,
    InvocationStatus,
    NullCoordinatorFactory,
    NullDeliverStore,
    NullNodeStateStore,
    RoutingError,
    SqliteDeliverStoreFactory,
    SqliteGraphInstanceStore,
    SqliteNodeStateStore,
    create_null_coordinator,
)

GID = 101


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


def _memory_coordinator(
    gid: int = GID,
) -> GraphPersistenceCoordinator:
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
    if db_path is None:
        import atexit
        import tempfile

        tmp_dir = tempfile.mkdtemp(prefix="modex_test_")
        db_path = str(Path(tmp_dir) / "instances.db")
        atexit.register(_cleanup_db_dir, tmp_dir)
    conn = conn or sqlite3.connect(db_path)
    coord = GraphPersistenceCoordinator(
        graph_instance_id=gid,
        instance_store=SqliteGraphInstanceStore(conn),
        node_state_store=SqliteNodeStateStore(conn, gid),
        default_deliver_store_factory=SqliteDeliverStoreFactory(conn),
    )
    return coord, conn, db_path


def _cleanup_db_dir(tmp_dir: str) -> None:
    import shutil

    shutil.rmtree(tmp_dir, ignore_errors=True)


# ── Constructor + register_node ───────────────────────────────────────────


class TestConstructorAndRegistration:
    def test_default_deliver_store_factory_is_required(self) -> None:
        with pytest.raises(TypeError):
            GraphPersistenceCoordinator(  # type: ignore[call-arg]
                graph_instance_id=GID,
                instance_store=InMemoryGraphInstanceStore(),
                node_state_store=InMemoryNodeStateStore(GID),
            )

    def test_register_node_uses_default_factory(self) -> None:
        coord = _memory_coordinator()
        coord.register_node("worker")
        assert coord.get_deliver_store("worker") is not None
        assert isinstance(coord.get_deliver_store("worker"), InMemoryDeliverStore)

    def test_register_node_uses_explicit_store(self) -> None:
        coord = _memory_coordinator()
        explicit_store = InMemoryDeliverStore()
        coord.register_node("worker", deliver_store=explicit_store)
        assert coord.get_deliver_store("worker") is explicit_store

    def test_node_state_store_property(self) -> None:
        coord = _memory_coordinator()
        assert coord.node_state_store is not None
        assert isinstance(coord.node_state_store, InMemoryNodeStateStore)

    def test_register_multiple_nodes(self) -> None:
        coord = _memory_coordinator()
        coord.register_node("node_a")
        coord.register_node("node_b")
        assert coord.get_deliver_store("node_a") is not coord.get_deliver_store("node_b")


# ── route_deliver ───────────────────────────────────────────────────────────


class TestRouteDeliver:
    def test_end_target_accumulates_in_registered_store(self) -> None:
        coord = _memory_coordinator()
        coord.register_node("end-node-id")
        store = coord.get_deliver_store("end-node-id")
        assert store is not None

        result = coord.route_deliver(
            target_node_id="end-node-id",
            content={"data": 1},
            source_node_id="worker",
            source_invocation_id=1001,
        )

        assert result is not None
        records = store.query_consumable(GID, "end-node-id")
        assert len(records) == 1
        assert records[0].content == {"data": 1}

    def test_route_to_registered_node_accumulates_deliver(self) -> None:
        coord = _memory_coordinator()
        coord.register_node("worker")
        store = coord.get_deliver_store("worker")
        assert store is not None

        deliver_id = coord.route_deliver(
            target_node_id="worker",
            content={"data": 1},
            source_node_id="source",
            source_invocation_id=1001,
        )
        assert deliver_id is not None
        assert deliver_id > 0

        records = store.query_consumable(GID, "worker")
        assert len(records) == 1
        assert records[0].deliver_id == deliver_id
        assert records[0].graph_instance_id == GID
        assert records[0].node_id == "worker"
        assert records[0].source_node_id == "source"
        assert records[0].source_invocation_id == 1001
        assert records[0].content == {"data": 1}
        assert records[0].status == DeliverConsumptionStatus.PENDING

    def test_route_to_unregistered_raises(self) -> None:
        coord = _memory_coordinator()
        coord.register_node("worker")
        with pytest.raises(RoutingError, match="no deliver_store"):
            coord.route_deliver(
                target_node_id="unknown",
                content={},
                source_node_id="source",
                source_invocation_id=1001,
            )


# ── Lifecycle via node_state_store ──────────────────────────────────────


class TestLifecycleViaStore:
    def test_begin_creates_running(self) -> None:
        coord = _memory_coordinator()
        coord.register_node("worker")
        store = coord.node_state_store

        inv = store.begin_invocation("worker")
        assert isinstance(inv, InvocationContext)
        assert inv.invocation_id > 0
        assert inv.version == 0
        assert inv.parent_version is None

    def test_complete_saves_completed(self) -> None:
        coord = _memory_coordinator()
        coord.register_node("worker")
        store = coord.node_state_store

        inv = store.begin_invocation("worker")
        store.complete_invocation(inv, {"result": "done"})

        latest = store.load_latest("worker")
        assert latest is not None
        assert latest.status == InvocationStatus.COMPLETED
        assert latest.state_json == {"result": "done"}

    def test_suspend_sets_suspended_true(self) -> None:
        coord = _memory_coordinator()
        coord.register_node("worker")
        store = coord.node_state_store

        inv = store.begin_invocation("worker")
        snapshot = {"resume_target": "tool_node", "intermediate": 42}
        store.suspend_invocation(inv, snapshot)

        latest = store.load_latest("worker")
        assert latest is not None
        assert latest.status == InvocationStatus.RUNNING
        assert latest.suspended is True
        assert latest.state_json == snapshot

    def test_cancel_saves_canceled(self) -> None:
        coord = _memory_coordinator()
        coord.register_node("worker")
        store = coord.node_state_store

        inv = store.begin_invocation("worker")
        store.cancel_invocation(inv)

        latest = store.load_latest("worker")
        assert latest is not None
        assert latest.status == InvocationStatus.CANCELED

    def test_crash_saves_crashed(self) -> None:
        coord = _memory_coordinator()
        coord.register_node("worker")
        store = coord.node_state_store

        inv = store.begin_invocation("worker")
        store.crash_invocation(inv)

        latest = store.load_latest("worker")
        assert latest is not None
        assert latest.status == InvocationStatus.CRASHED

    def test_finalize_orphan_to_crashed(self) -> None:
        coord = _memory_coordinator()
        coord.register_node("worker")
        store = coord.node_state_store

        inv = store.begin_invocation("worker")
        store.finalize_invocation(inv)

        latest = store.load_latest("worker")
        assert latest is not None
        assert latest.status == InvocationStatus.CRASHED

    def test_finalize_skips_suspended(self) -> None:
        coord = _memory_coordinator()
        coord.register_node("worker")
        store = coord.node_state_store

        inv = store.begin_invocation("worker")
        store.suspend_invocation(inv, {"snapshot": True})
        store.finalize_invocation(inv)

        latest = store.load_latest("worker")
        assert latest is not None
        assert latest.status == InvocationStatus.RUNNING
        assert latest.suspended is True

    def test_suspended_running_left_in_place_on_begin(self) -> None:
        coord = _memory_coordinator()
        coord.register_node("worker")
        store = coord.node_state_store

        inv0 = store.begin_invocation("worker")
        store.suspend_invocation(inv0, {"resume_target": "tool"})

        store.begin_invocation("worker")

        running = store.query_versions("worker", {InvocationStatus.RUNNING})
        suspended_records = [r for r in running if r.suspended]
        assert len(suspended_records) == 1
        assert suspended_records[0].invocation_id == inv0.invocation_id
        assert suspended_records[0].state_json == {"resume_target": "tool"}

    def test_orphan_running_marked_crashed_on_begin(self) -> None:
        coord = _memory_coordinator()
        coord.register_node("worker")
        store = coord.node_state_store

        inv0 = store.begin_invocation("worker")
        store.begin_invocation("worker")

        crashed = store.query_versions("worker", {InvocationStatus.CRASHED})
        assert len(crashed) == 1
        assert crashed[0].invocation_id == inv0.invocation_id

    def test_version_is_max_plus_one(self) -> None:
        coord = _memory_coordinator()
        coord.register_node("worker")
        store = coord.node_state_store

        inv0 = store.begin_invocation("worker")
        store.crash_invocation(inv0)

        inv1 = store.begin_invocation("worker")
        assert inv1.version == 1
        store.complete_invocation(inv1, {"step": 1})

        inv2 = store.begin_invocation("worker")
        assert inv2.version == 2

    def test_parent_version_from_latest_completed(self) -> None:
        coord = _memory_coordinator()
        coord.register_node("worker")
        store = coord.node_state_store

        inv0 = store.begin_invocation("worker")
        store.complete_invocation(inv0, {"step": 0})

        inv1 = store.begin_invocation("worker")
        assert inv1.parent_version == 0


# ── promote_delivers ──────────────────────────────────────────────────────


class TestPromoteDelivers:
    def test_complete_with_promote(self) -> None:
        coord = _memory_coordinator()
        coord.register_node("worker")
        store = coord.node_state_store

        inv = store.begin_invocation("worker")
        store.complete_invocation(inv, {"result": "done"})
        coord.promote_delivers("worker", inv.invocation_id)

    def test_promote_all_consumed_pending_for_node(self) -> None:
        coord, conn, db_path = _sqlite_coordinator()
        coord.register_node("worker")
        store = coord.node_state_store

        inv0 = store.begin_invocation("worker")
        store.complete_invocation(inv0, {"step": 0})

        d1 = coord.route_deliver("worker", {"data": 1}, "source", 9999)
        d2 = coord.route_deliver("worker", {"data": 2}, "source", 9999)
        assert d1 is not None and d2 is not None

        deliver_store = coord.get_deliver_store("worker")
        assert deliver_store is not None
        deliver_store.mark_consumed([d1, d2], inv0.invocation_id)

        consumable_before = deliver_store.query_consumable(GID, "worker")
        pending_count = sum(
            1 for r in consumable_before if r.status == DeliverConsumptionStatus.CONSUMED_PENDING
        )
        assert pending_count == 2

        inv1 = store.begin_invocation("worker")
        store.complete_invocation(inv1, {"step": 1})
        coord.promote_delivers("worker", inv1.invocation_id)

        consumable_after = deliver_store.query_consumable(GID, "worker")
        for r in consumable_after:
            assert r.status != DeliverConsumptionStatus.CONSUMED_PENDING


# ── rebuild_main_state ────────────────────────────────────────────────────


class TestRebuildMainState:
    def test_global_invocation_id_order(self) -> None:
        coord = _memory_coordinator()
        coord.register_node("node_a")
        coord.register_node("node_b")
        store = coord.node_state_store

        inv_a = store.begin_invocation("node_a")
        store.complete_invocation(inv_a, {"a_value": 1})

        inv_b = store.begin_invocation("node_b")
        store.complete_invocation(inv_b, {"b_value": 2, "a_value": 99})

        state = coord.rebuild_main_state()
        assert state["a_value"] == 99
        assert state["b_value"] == 2

    def test_newer_completed_beats_older(self) -> None:
        coord = _memory_coordinator()
        coord.register_node("worker")
        store = coord.node_state_store

        inv0 = store.begin_invocation("worker")
        store.complete_invocation(inv0, {"version": "old", "kept": True})

        inv1 = store.begin_invocation("worker")
        store.complete_invocation(inv1, {"version": "new"})

        state = coord.rebuild_main_state()
        assert state == {"version": "new"}

    def test_suspended_running_beats_older_completed(self) -> None:
        coord = _memory_coordinator()
        coord.register_node("worker")
        store = coord.node_state_store

        inv0 = store.begin_invocation("worker")
        store.complete_invocation(inv0, {"base": 1, "override": "original"})

        inv1 = store.begin_invocation("worker")
        store.suspend_invocation(inv1, {"override": "suspended", "resume_target": "tool"})

        state = coord.rebuild_main_state()
        assert state == {"override": "suspended", "resume_target": "tool"}

    def test_empty_state_when_no_completed(self) -> None:
        coord = _memory_coordinator()
        coord.register_node("worker")
        assert coord.rebuild_main_state() == {}


# ── get_graph_state ────────────────────────────────────────────────────────


class TestGetGraphState:
    def test_returns_graph_state_snapshot(self) -> None:
        coord = _memory_coordinator()
        coord.register_node("worker")
        coord._instance_store.save(_metadata())
        store = coord.node_state_store

        inv = store.begin_invocation("worker")
        store.complete_invocation(inv, {"result": "done"})

        snapshot = coord.get_graph_state()
        assert isinstance(snapshot, GraphStateSnapshot)
        assert snapshot.metadata.graph_instance_id == GID
        assert "worker" in snapshot.nodes
        assert len(snapshot.nodes["worker"]) >= 1

    def test_status_filter(self) -> None:
        coord = _memory_coordinator()
        coord.register_node("worker")
        coord._instance_store.save(_metadata())
        store = coord.node_state_store

        inv0 = store.begin_invocation("worker")
        store.complete_invocation(inv0, {"step": 0})
        inv1 = store.begin_invocation("worker")
        store.crash_invocation(inv1)

        snapshot = coord.get_graph_state({InvocationStatus.COMPLETED})
        assert len(snapshot.nodes["worker"]) == 1
        assert snapshot.nodes["worker"][0].status == InvocationStatus.COMPLETED


# ── Version Chain ──────────────────────────────────────────────────────────


class TestVersionChain:
    def test_version_chain_progression(self) -> None:
        coord = _memory_coordinator()
        coord.register_node("worker")
        store = coord.node_state_store

        inv0 = store.begin_invocation("worker")
        assert inv0.version == 0
        assert inv0.parent_version is None
        store.complete_invocation(inv0, {"v": 0})

        inv1 = store.begin_invocation("worker")
        assert inv1.version == 1
        assert inv1.parent_version == 0
        store.complete_invocation(inv1, {"v": 1})

        inv2 = store.begin_invocation("worker")
        assert inv2.version == 2
        assert inv2.parent_version == 1
        store.crash_invocation(inv2)

        inv3 = store.begin_invocation("worker")
        assert inv3.version == 3
        assert inv3.parent_version == 1

    def test_parent_version_points_to_latest_completed(self) -> None:
        coord = _memory_coordinator()
        coord.register_node("worker")
        store = coord.node_state_store

        inv0 = store.begin_invocation("worker")
        store.complete_invocation(inv0, {"v": 0})

        inv1 = store.begin_invocation("worker")
        store.crash_invocation(inv1)

        inv2 = store.begin_invocation("worker")
        assert inv2.parent_version == 0
        assert inv2.parent_version != 1


# ── SQLite Lifecycle Integration ──────────────────────────────────────────


class TestSqliteLifecycle:
    def test_sqlite_begin_complete_lifecycle(self) -> None:
        coord, _, _ = _sqlite_coordinator()
        coord.register_node("worker")
        coord._instance_store.save(_metadata())
        store = coord.node_state_store

        inv = store.begin_invocation("worker")
        store.complete_invocation(inv, {"result": "done"})

        latest = store.load_latest("worker")
        assert latest is not None
        assert latest.status == InvocationStatus.COMPLETED
        assert latest.state_json == {"result": "done"}

    def test_sqlite_suspend_resume_lifecycle(self) -> None:
        coord, _, _ = _sqlite_coordinator()
        coord.register_node("worker")
        store = coord.node_state_store

        inv0 = store.begin_invocation("worker")
        store.suspend_invocation(inv0, {"resume_target": "tool"})

        inv1 = store.begin_invocation("worker")
        assert inv1.version == 1
        assert inv1.parent_version is None

        running = store.query_versions("worker", {InvocationStatus.RUNNING})
        suspended_records = [r for r in running if r.suspended]
        assert len(suspended_records) == 1

        store.complete_invocation(inv1, {"result": "resumed"})

        latest = store.load_latest("worker")
        assert latest is not None
        assert latest.status == InvocationStatus.COMPLETED

    def test_sqlite_recovery_preserves_state(self) -> None:
        coord, conn, db_path = _sqlite_coordinator()
        coord.register_node("worker")
        coord._instance_store.save(_metadata())
        store = coord.node_state_store

        inv = store.begin_invocation("worker")
        store.complete_invocation(inv, {"result": "persisted"})

        coord2, _, _ = _sqlite_coordinator(conn=conn, db_path=db_path)
        coord2.register_node("worker")

        assert coord2.rebuild_main_state() == {"result": "persisted"}
        latest = coord2.node_state_store.load_latest("worker")
        assert latest is not None
        assert latest.status == InvocationStatus.COMPLETED


# ── close ──────────────────────────────────────────────────────────────────


class TestClose:
    def test_close_is_safe_to_call(self) -> None:
        coord, conn, db_path = _sqlite_coordinator()
        coord.register_node("worker")
        coord.close()
        coord.close()

    def test_close_with_memory_stores(self) -> None:
        coord = _memory_coordinator()
        coord.register_node("worker")
        coord.close()


# ── Null Strategy ──────────────────────────────────────────────────────────


class TestNullStrategy:
    def test_null_coordinator_lifecycle(self) -> None:
        coord = create_null_coordinator(GID)
        coord.register_node("worker")
        store = coord.node_state_store

        inv = store.begin_invocation("worker")
        assert inv.invocation_id > 0
        assert inv.version == 0

        store.complete_invocation(inv, {"result": "done"})

        latest = store.load_latest("worker")
        assert latest is None

        assert coord.rebuild_main_state() == {}

    def test_null_route_deliver_to_registered_end(self) -> None:
        coord = create_null_coordinator(GID)
        coord.register_node("end-node-id")
        assert coord.route_deliver("end-node-id", {}, "src", 1) is not None

    def test_null_route_deliver_accumulates(self) -> None:
        coord = create_null_coordinator(GID)
        coord.register_node("worker")
        deliver_id = coord.route_deliver("worker", {"data": 1}, "src", 1)
        assert deliver_id is not None
        assert deliver_id > 0

    def test_null_uses_null_node_state_store(self) -> None:
        coord = create_null_coordinator(GID)
        assert isinstance(coord.node_state_store, NullNodeStateStore)


# ── CoordinatorFactory ABC + NullCoordinatorFactory ──────────────────────


class TestCoordinatorFactory:
    def test_abc_cannot_be_instantiated_directly(self) -> None:
        with pytest.raises(TypeError):
            CoordinatorFactory()  # type: ignore[abstract]

    def test_null_factory_returns_coordinator(self) -> None:
        store = InMemoryGraphInstanceStore()
        coord = NullCoordinatorFactory().create(GID, store)
        assert isinstance(coord, GraphPersistenceCoordinator)

    def test_null_factory_uses_passed_instance_store(self) -> None:
        store = InMemoryGraphInstanceStore()
        store.save(_metadata(gid=GID))
        coord = NullCoordinatorFactory().create(GID, store)
        metadata = coord._instance_store.load(GID)
        assert metadata is not None
        assert metadata.graph_instance_id == GID

    def test_null_factory_uses_null_node_state_store(self) -> None:
        store = InMemoryGraphInstanceStore()
        coord = NullCoordinatorFactory().create(GID, store)
        assert isinstance(coord.node_state_store, NullNodeStateStore)

    def test_null_factory_uses_null_deliver_store_factory(self) -> None:
        store = InMemoryGraphInstanceStore()
        coord = NullCoordinatorFactory().create(GID, store)
        coord.register_node("worker")
        ds = coord.get_deliver_store("worker")
        assert ds is not None
        assert isinstance(ds, NullDeliverStore)
        deliver_id = ds.accumulate(
            graph_instance_id=GID,
            node_id="worker",
            source_node_id="src",
            source_invocation_id=1,
            content={"x": 1},
        )
        assert deliver_id is not None
        assert deliver_id > 0

    def test_null_factory_differs_from_create_null_coordinator(self) -> None:
        """Factory shares the caller's instance_store; create_null_coordinator
        uses a NullGraphInstanceStore that loses all metadata."""
        store = InMemoryGraphInstanceStore()
        store.save(_metadata(gid=GID))

        factory_coord = NullCoordinatorFactory().create(GID, store)
        null_coord = create_null_coordinator(GID)

        # Factory shares the caller's instance_store: metadata is available.
        factory_metadata = factory_coord._instance_store.load(GID)
        assert factory_metadata is not None
        assert factory_metadata.graph_instance_id == GID

        # create_null_coordinator uses NullGraphInstanceStore: load returns None.
        assert null_coord._instance_store.load(GID) is None
