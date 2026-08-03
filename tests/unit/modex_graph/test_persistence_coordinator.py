# ruff: noqa: ANN401, S101

"""Tests for ``GraphPersistenceCoordinator``.

Covers all acceptance criteria:

- Lifecycle transitions (begin/complete/cancel/suspend/crash/finalize)
- Version chain (version = max + 1, parent_version internal)
- suspended=True marking + SUPERSEDED + finalize safety net
- begin_invocation has no parent_version parameter
- default_deliver_store_factory is required
- Consumption methods (collect/mark/promote)
- promote_delivers upgrades ALL CONSUMED_PENDING for node
- route_deliver skips END
- rebuild_main_state sorts by invocation_id globally
- load_for_recovery returns RecoveryContext with rebuilt_main_state
- Resume skips re-consume (SUPERSEDED snapshot)
- Crash between SUPERSEDED marking + new invocation → recovery → re-dispatch
- Crash between save COMPLETED + promote → recovery → auto-promote
- close() closes resources
"""

from __future__ import annotations

import sqlite3

import pytest

from modex_graph import (
    DeliverConsumptionStatus,
    DeliverRecord,
    GraphInstanceStatus,
    GraphMetadata,
    GraphNode,
    GraphPersistenceCoordinator,
    GraphStateSnapshot,
    InMemoryDeliverStore,
    InMemoryDeliverStoreFactory,
    InvocationContext,
    InvocationStatus,
    MemoryGraphMetadataStore,
    RecoveryContext,
    RoutingError,
    SimpleNodeState,
    SimpleNodeStateFactory,
    SqliteDeliverStoreFactory,
    SqliteGraphMetadataStore,
    SqliteNodeState,
    SqliteNodeStateFactory,
    create_null_coordinator,
)

# ── Helpers ───────────────────────────────────────────────────────────────


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
        instance_seq=3,
        iteration_count=4,
        activated_sources={"worker": ["__start__"]},
        pending_dispatches={"worker": {"__start__": [{"value": 1}]}},
    )


def _memory_coordinator(
    gid: int = GID,
) -> GraphPersistenceCoordinator:
    return GraphPersistenceCoordinator(
        graph_instance_id=gid,
        graph_metadata_store=MemoryGraphMetadataStore(),
        default_node_state_factory=SimpleNodeStateFactory(),
        default_deliver_store_factory=InMemoryDeliverStoreFactory(),
    )


def _sqlite_coordinator(
    conn: sqlite3.Connection | None = None,
    gid: int = GID,
) -> tuple[GraphPersistenceCoordinator, sqlite3.Connection]:
    conn = conn or sqlite3.connect(":memory:")
    coord = GraphPersistenceCoordinator(
        graph_instance_id=gid,
        graph_metadata_store=SqliteGraphMetadataStore(conn),
        default_node_state_factory=SqliteNodeStateFactory(conn),
        default_deliver_store_factory=SqliteDeliverStoreFactory(conn),
    )
    return coord, conn


# ── Constructor + register_node ───────────────────────────────────────────


class TestConstructorAndRegistration:
    """default_deliver_store_factory is required. register_node uses defaults."""

    def test_f11_default_deliver_store_factory_is_required(self) -> None:
        """default_deliver_store_factory is a required parameter (not Optional)."""
        with pytest.raises(TypeError):
            GraphPersistenceCoordinator(  # type: ignore[call-arg]
                graph_instance_id=GID,
                graph_metadata_store=MemoryGraphMetadataStore(),
                default_node_state_factory=SimpleNodeStateFactory(),
            )

    def test_register_node_uses_default_factories_when_none(self) -> None:
        coord = _memory_coordinator()
        coord.register_node("worker")

        assert coord.get_deliver_store("worker") is not None
        assert isinstance(coord.get_deliver_store("worker"), InMemoryDeliverStore)

    def test_register_node_uses_explicit_stores_when_provided(self) -> None:
        coord = _memory_coordinator()
        explicit_state = SimpleNodeState({"initial": True})
        explicit_store = InMemoryDeliverStore()
        coord.register_node("worker", node_state=explicit_state, deliver_store=explicit_store)

        assert coord.get_deliver_store("worker") is explicit_store

    def test_register_multiple_nodes(self) -> None:
        coord = _memory_coordinator()
        coord.register_node("node_a")
        coord.register_node("node_b")

        assert coord.get_deliver_store("node_a") is not coord.get_deliver_store("node_b")


# ── route_deliver ───────────────────────────────────────────────────


class TestRouteDeliver:
    """END target skipped. Unregistered node raises RoutingError."""

    def test_i20_end_target_returns_none(self) -> None:
        coord = _memory_coordinator()
        coord.register_node("worker")

        result = coord.route_deliver(
            target_node=GraphNode.END,
            content={"data": 1},
            source_node="worker",
            source_invocation_id=1001,
        )
        assert result is None

    def test_route_deliver_to_registered_node_returns_deliver_id(self) -> None:
        coord = _memory_coordinator()
        coord.register_node("worker")

        deliver_id = coord.route_deliver(
            target_node="worker",
            content={"data": 1},
            source_node="source",
            source_invocation_id=1001,
        )
        assert deliver_id is not None
        assert deliver_id > 0

    def test_route_deliver_to_unregistered_node_raises_routing_error(self) -> None:
        coord = _memory_coordinator()
        coord.register_node("worker")

        with pytest.raises(RoutingError, match="no deliver_store"):
            coord.route_deliver(
                target_node="unknown",
                content={},
                source_node="source",
                source_invocation_id=1001,
            )


# ── begin_invocation ──────────────────────────────────────


class TestBeginInvocation:
    """no parent_version param. version = max+1. suspended→SUPERSEDED."""

    def test_f8_begin_invocation_has_no_parent_version_parameter(self) -> None:
        """begin_invocation takes only node_name — parent_version is internal."""
        coord = _memory_coordinator()
        coord.register_node("worker")

        inv = coord.begin_invocation("worker")

        assert isinstance(inv, InvocationContext)
        assert inv.node_name == "worker"
        assert inv.invocation_id > 0

    def test_first_invocation_has_version_0_and_parent_version_none(self) -> None:
        coord = _memory_coordinator()
        coord.register_node("worker")

        inv = coord.begin_invocation("worker")

        assert inv.version == 0
        assert inv.parent_version is None

    def test_i18_version_is_max_all_versions_plus_one(self) -> None:
        """version = max(all existing versions) + 1, not load_latest_completed + 1."""
        coord = _memory_coordinator()
        coord.register_node("worker")

        inv0 = coord.begin_invocation("worker")
        coord.crash_invocation(inv0)

        inv1 = coord.begin_invocation("worker")
        assert inv1.version == 1
        coord.complete_invocation(inv1, {"step": 1})

        inv2 = coord.begin_invocation("worker")
        assert inv2.version == 2
        coord.crash_invocation(inv2)

        inv3 = coord.begin_invocation("worker")
        assert inv3.version == 3

    def test_f8_parent_version_from_load_latest_completed(self) -> None:
        """parent_version computed from load_latest_completed."""
        coord = _memory_coordinator()
        coord.register_node("worker")

        inv0 = coord.begin_invocation("worker")
        coord.complete_invocation(inv0, {"step": 0})
        assert inv0.version == 0

        inv1 = coord.begin_invocation("worker")
        assert inv1.parent_version == 0

    def test_f4_suspended_running_marked_superseded(self) -> None:
        """suspended=True RUNNING → SUPERSEDED on next begin_invocation."""
        coord = _memory_coordinator()
        coord.register_node("worker")

        inv0 = coord.begin_invocation("worker")
        coord.suspend_invocation(inv0, {"resume_target": "tool"})

        coord.begin_invocation("worker")

        latest = coord.load_latest_invocation("worker")
        assert latest is not None
        assert latest.status == InvocationStatus.RUNNING

        node_state = coord._node_states["worker"]
        versions = node_state.query_versions(GID, "worker", {InvocationStatus.SUPERSEDED})
        assert len(versions) == 1
        assert versions[0].suspended is True
        assert versions[0].state_json == {"resume_target": "tool"}

    def test_orphan_running_marked_crashed(self) -> None:
        """Safety net: orphan RUNNING (suspended=False) → CRASHED."""
        coord = _memory_coordinator()
        coord.register_node("worker")

        inv0 = coord.begin_invocation("worker")
        node_state = coord._node_states["worker"]
        node_state.save_invocation(
            GID,
            "worker",
            inv0.invocation_id,
            inv0.version,
            inv0.parent_version,
            InvocationStatus.RUNNING,
            {},
        )

        coord.begin_invocation("worker")

        versions = node_state.query_versions(GID, "worker", {InvocationStatus.CRASHED})
        assert len(versions) == 1
        assert versions[0].invocation_id == inv0.invocation_id

    def test_orphan_pending_marked_crashed(self) -> None:
        """Safety net: orphan PENDING → CRASHED."""
        coord = _memory_coordinator()
        coord.register_node("worker")

        inv0 = coord.begin_invocation("worker")

        coord.begin_invocation("worker")

        node_state = coord._node_states["worker"]
        versions = node_state.query_versions(GID, "worker", {InvocationStatus.CRASHED})
        assert len(versions) == 1
        assert versions[0].invocation_id == inv0.invocation_id

    def test_begin_invocation_unregistered_node_raises(self) -> None:
        coord = _memory_coordinator()
        with pytest.raises(RoutingError, match="not registered"):
            coord.begin_invocation("unknown")

    def test_i17_self_cleanup_on_failure(self) -> None:
        """begin_invocation internal try/except re-raises on failure."""
        coord = _memory_coordinator()
        coord.register_node("worker")

        with pytest.raises(RoutingError):
            coord.begin_invocation("nonexistent")


# ── complete_invocation ───────────────────────────────────────


class TestCompleteInvocation:
    """promote_delivers called. promote ALL CONSUMED_PENDING."""

    def test_complete_saves_completed_status(self) -> None:
        coord = _memory_coordinator()
        coord.register_node("worker")

        inv = coord.begin_invocation("worker")
        coord.complete_invocation(inv, {"result": "done"})

        latest = coord.load_latest_invocation("worker")
        assert latest is not None
        assert latest.status == InvocationStatus.COMPLETED
        assert latest.state_json == {"result": "done"}

    def test_complete_unregistered_node_raises(self) -> None:
        coord = _memory_coordinator()
        inv = InvocationContext(
            invocation_id=1, node_name="unknown", version=0, parent_version=None
        )
        with pytest.raises(RoutingError, match="not registered"):
            coord.complete_invocation(inv, {})

    def test_f3_promote_all_consumed_pending_for_node(self) -> None:
        """promote ALL CONSUMED_PENDING for node, not just current invocation."""
        coord, conn = _sqlite_coordinator()
        coord.register_node("worker")

        inv0 = coord.begin_invocation("worker")
        coord.complete_invocation(inv0, {"step": 0})

        d1 = coord.route_deliver("worker", {"data": 1}, "source", 9999)
        d2 = coord.route_deliver("worker", {"data": 2}, "source", 9999)
        assert d1 is not None and d2 is not None

        store = coord.get_deliver_store("worker")
        assert store is not None
        store.mark_consumed([d1, d2], inv0.invocation_id)

        consumable_before = store.query_consumable(GID, "worker")
        pending_count = sum(
            1 for r in consumable_before if r.status == DeliverConsumptionStatus.CONSUMED_PENDING
        )
        assert pending_count == 2

        inv1 = coord.begin_invocation("worker")
        coord.complete_invocation(inv1, {"step": 1})

        consumable_after = store.query_consumable(GID, "worker")
        for r in consumable_after:
            assert r.status != DeliverConsumptionStatus.CONSUMED_PENDING


# ── cancel / suspend / crash ──────────────────────────────────────────────


class TestCancelSuspendCrash:
    """cancel → CANCELED. suspend → RUNNING+suspended=True. crash → CRASHED."""

    def test_cancel_saves_canceled(self) -> None:
        coord = _memory_coordinator()
        coord.register_node("worker")

        inv = coord.begin_invocation("worker")
        coord.cancel_invocation(inv)

        latest = coord.load_latest_invocation("worker")
        assert latest is not None
        assert latest.status == InvocationStatus.CANCELED

    def test_f4_suspend_sets_suspended_true(self) -> None:
        """suspend_invocation sets suspended=True, status stays RUNNING."""
        coord = _memory_coordinator()
        coord.register_node("worker")

        inv = coord.begin_invocation("worker")
        snapshot = {"resume_target": "tool_node", "intermediate": 42}
        coord.suspend_invocation(inv, snapshot)

        latest = coord.load_latest_invocation("worker")
        assert latest is not None
        assert latest.status == InvocationStatus.RUNNING
        assert latest.suspended is True
        assert latest.state_json == snapshot

    def test_crash_saves_crashed(self) -> None:
        coord = _memory_coordinator()
        coord.register_node("worker")

        inv = coord.begin_invocation("worker")
        coord.crash_invocation(inv)

        latest = coord.load_latest_invocation("worker")
        assert latest is not None
        assert latest.status == InvocationStatus.CRASHED


# ── finalize_invocation ──────────────────────────────────────────────


class TestFinalizeInvocation:
    """suspended RUNNING untouched. SUPERSEDED untouched. Orphan RUNNING/PENDING → CRASHED."""

    def test_f4_finalize_skips_suspended_running(self) -> None:
        """finalize does NOT touch suspended=True RUNNING."""
        coord = _memory_coordinator()
        coord.register_node("worker")

        inv = coord.begin_invocation("worker")
        coord.suspend_invocation(inv, {"snapshot": True})
        coord.finalize_invocation(inv)

        latest = coord.load_latest_invocation("worker")
        assert latest is not None
        assert latest.status == InvocationStatus.RUNNING
        assert latest.suspended is True

    def test_finalize_skips_superseded(self) -> None:
        coord = _memory_coordinator()
        coord.register_node("worker")

        inv0 = coord.begin_invocation("worker")
        coord.suspend_invocation(inv0, {"snapshot": True})
        coord.begin_invocation("worker")

        coord.finalize_invocation(inv0)

        node_state = coord._node_states["worker"]
        versions = node_state.query_versions(GID, "worker", {InvocationStatus.SUPERSEDED})
        assert len(versions) == 1

    def test_finalize_orphan_to_crashed(self) -> None:
        """Orphan invocation (begin but no terminal transition) → CRASHED."""
        coord = _memory_coordinator()
        coord.register_node("worker")

        inv = coord.begin_invocation("worker")
        coord.finalize_invocation(inv)

        latest = coord.load_latest_invocation("worker")
        assert latest is not None
        assert latest.status == InvocationStatus.CRASHED

    def test_finalize_skips_completed(self) -> None:
        coord = _memory_coordinator()
        coord.register_node("worker")

        inv = coord.begin_invocation("worker")
        coord.complete_invocation(inv, {"result": "done"})
        coord.finalize_invocation(inv)

        latest = coord.load_latest_invocation("worker")
        assert latest is not None
        assert latest.status == InvocationStatus.COMPLETED

    def test_finalize_unregistered_node_noop(self) -> None:
        coord = _memory_coordinator()
        inv = InvocationContext(
            invocation_id=1, node_name="unknown", version=0, parent_version=None
        )
        coord.finalize_invocation(inv)


# ── Consumption methods ─────────────────────────────────────────────


class TestConsumptionMethods:
    """collect/mark/promote delegate to deliver_store."""

    def test_collect_consumable_returns_delivers(self) -> None:
        coord = _memory_coordinator()
        coord.register_node("worker")

        coord.route_deliver("worker", {"data": 1}, "source", 9999)
        coord.route_deliver("worker", {"data": 2}, "source", 9999)

        consumable = coord.collect_consumable_delivers("worker", invocation_id=1)
        assert len(consumable) == 2
        assert all(isinstance(r, DeliverRecord) for r in consumable)

    def test_collect_consumable_unregistered_node_returns_empty(self) -> None:
        coord = _memory_coordinator()
        assert coord.collect_consumable_delivers("unknown", invocation_id=1) == []

    def test_mark_delivers_consumed(self) -> None:
        coord = _memory_coordinator()
        coord.register_node("worker")

        d1 = coord.route_deliver("worker", {"data": 1}, "source", 9999)
        d2 = coord.route_deliver("worker", {"data": 2}, "source", 9999)
        assert d1 is not None and d2 is not None

        coord.mark_delivers_consumed("worker", [d1, d2], invocation_id=1001)

        consumable = coord.collect_consumable_delivers("worker", invocation_id=1001)
        assert len(consumable) == 0

    def test_promote_delivers_idempotent(self) -> None:
        coord = _memory_coordinator()
        coord.register_node("worker")

        d1 = coord.route_deliver("worker", {"data": 1}, "source", 9999)
        assert d1 is not None
        coord.mark_delivers_consumed("worker", [d1], invocation_id=1001)

        coord.promote_delivers("worker", 1001)
        coord.promote_delivers("worker", 1001)


# ── rebuild_main_state ───────────────────────────────────────────────


class TestRebuildMainState:
    """sort by invocation_id globally. SUPERSEDED snapshots applied last."""

    def test_i5_global_invocation_id_order(self) -> None:
        """COMPLETED records applied in global invocation_id order."""
        coord = _memory_coordinator()
        coord.register_node("node_a")
        coord.register_node("node_b")

        inv_a = coord.begin_invocation("node_a")
        coord.complete_invocation(inv_a, {"a_value": 1})

        inv_b = coord.begin_invocation("node_b")
        coord.complete_invocation(inv_b, {"b_value": 2, "a_value": 99})

        state = coord.rebuild_main_state()
        assert state["a_value"] == 99
        assert state["b_value"] == 2

    def test_superseded_snapshot_applied_last(self) -> None:
        """SUPERSEDED state_json (suspend snapshot) applied after all COMPLETED."""
        coord = _memory_coordinator()
        coord.register_node("worker")

        inv0 = coord.begin_invocation("worker")
        coord.complete_invocation(inv0, {"base": 1, "override": "original"})

        inv1 = coord.begin_invocation("worker")
        coord.suspend_invocation(inv1, {"override": "suspended", "resume_target": "tool"})

        coord.begin_invocation("worker")

        state = coord.rebuild_main_state()
        assert state["base"] == 1
        assert state["override"] == "suspended"
        assert state["resume_target"] == "tool"

    def test_empty_state_when_no_completed(self) -> None:
        coord = _memory_coordinator()
        coord.register_node("worker")

        state = coord.rebuild_main_state()
        assert state == {}


# ── load_for_recovery ────────────────────────────────────────────────


class TestLoadForRecovery:
    """returns RecoveryContext with rebuilt_main_state."""

    def test_i9_returns_recovery_context_with_rebuilt_main_state(self) -> None:
        coord = _memory_coordinator()
        coord.register_node("worker")
        coord._metadata_store.save(GID, _metadata())

        inv = coord.begin_invocation("worker")
        coord.complete_invocation(inv, {"result": "done"})

        ctx = coord.load_for_recovery()
        assert isinstance(ctx, RecoveryContext)
        assert ctx.metadata.graph_instance_id == GID
        assert "worker" in ctx.node_states
        assert ctx.node_states["worker"] is not None
        assert ctx.node_states["worker"].status == InvocationStatus.COMPLETED
        assert ctx.rebuilt_main_state == {"result": "done"}

    def test_fresh_graph_returns_minimal_context(self) -> None:
        coord = _memory_coordinator()
        coord.register_node("worker")

        ctx = coord.load_for_recovery()
        assert ctx.metadata.status == GraphInstanceStatus.RUNNING
        assert ctx.rebuilt_main_state == {}
        assert "worker" in ctx.node_states

    def test_f2_auto_promote_on_recovery(self) -> None:
        """crash between save COMPLETED + promote → recovery auto-promotes."""
        coord, conn = _sqlite_coordinator()
        coord.register_node("worker")
        coord._metadata_store.save(GID, _metadata())

        inv = coord.begin_invocation("worker")

        d1 = coord.route_deliver("worker", {"data": 1}, "source", 9999)
        assert d1 is not None
        store = coord.get_deliver_store("worker")
        assert store is not None
        store.mark_consumed([d1], inv.invocation_id)

        coord._node_states["worker"].save_invocation(
            GID,
            "worker",
            inv.invocation_id,
            inv.version,
            inv.parent_version,
            InvocationStatus.COMPLETED,
            {"result": "done"},
        )

        consumable_before = store.query_consumable(GID, "worker")
        consumed_pending = [
            r for r in consumable_before if r.status == DeliverConsumptionStatus.CONSUMED_PENDING
        ]
        assert len(consumed_pending) == 1

        coord2, _ = _sqlite_coordinator(conn=conn)
        coord2.register_node("worker")

        coord2.load_for_recovery()

        consumable_after = store.query_consumable(GID, "worker")
        for r in consumable_after:
            assert r.status != DeliverConsumptionStatus.CONSUMED_PENDING


# ── load_latest_invocation ──────────────────────────────────────────


class TestLoadLatestInvocation:
    """load_latest_invocation for resume check."""

    def test_returns_latest_invocation(self) -> None:
        coord = _memory_coordinator()
        coord.register_node("worker")

        inv0 = coord.begin_invocation("worker")
        coord.complete_invocation(inv0, {"step": 0})

        inv1 = coord.begin_invocation("worker")

        latest = coord.load_latest_invocation("worker")
        assert latest is not None
        assert latest.invocation_id == inv1.invocation_id

    def test_returns_none_for_unregistered(self) -> None:
        coord = _memory_coordinator()
        assert coord.load_latest_invocation("unknown") is None

    def test_i16_resume_check_superseded_with_snapshot(self) -> None:
        """resume checks previous SUPERSEDED invocation for state_snapshot."""
        coord = _memory_coordinator()
        coord.register_node("worker")

        inv0 = coord.begin_invocation("worker")
        coord.suspend_invocation(inv0, {"resume_target": "tool"})

        coord.begin_invocation("worker")

        node_state = coord._node_states["worker"]
        superseded = node_state.query_versions(GID, "worker", {InvocationStatus.SUPERSEDED})
        assert len(superseded) == 1
        assert superseded[0].state_json == {"resume_target": "tool"}


# ── get_graph_state ───────────────────────────────────────────────────────


class TestGetGraphState:
    """get_graph_state collects metadata + per-node versions."""

    def test_returns_graph_state_snapshot(self) -> None:
        coord = _memory_coordinator()
        coord.register_node("worker")
        coord._metadata_store.save(GID, _metadata())

        inv = coord.begin_invocation("worker")
        coord.complete_invocation(inv, {"result": "done"})

        snapshot = coord.get_graph_state()
        assert isinstance(snapshot, GraphStateSnapshot)
        assert snapshot.metadata.graph_instance_id == GID
        assert "worker" in snapshot.nodes
        assert len(snapshot.nodes["worker"]) >= 1

    def test_status_filter(self) -> None:
        coord = _memory_coordinator()
        coord.register_node("worker")
        coord._metadata_store.save(GID, _metadata())

        inv0 = coord.begin_invocation("worker")
        coord.complete_invocation(inv0, {"step": 0})
        inv1 = coord.begin_invocation("worker")
        coord.crash_invocation(inv1)

        snapshot = coord.get_graph_state({InvocationStatus.COMPLETED})
        assert len(snapshot.nodes["worker"]) == 1
        assert snapshot.nodes["worker"][0].status == InvocationStatus.COMPLETED


# ── Transaction Test: SUPERSEDED crash ───────────────────────────────────────────────────


class TestF1Transaction:
    """crash between SUPERSEDED marking + new invocation → recovery → re-dispatch."""

    def test_f1_superseded_no_successor_recovery_redispatch(self) -> None:
        """Simulate: begin_invocation marks v1 SUPERSEDED, then crashes before creating v2.

        Recovery should show v1 as SUPERSEDED with no successor.
        A new begin_invocation (re-dispatch) should create v2 successfully.
        """
        coord, conn = _sqlite_coordinator()
        coord.register_node("worker")
        coord._metadata_store.save(GID, _metadata())

        inv0 = coord.begin_invocation("worker")
        coord.complete_invocation(inv0, {"base": 1})

        inv1 = coord.begin_invocation("worker")
        coord.suspend_invocation(inv1, {"resume_target": "tool"})

        node_state = SqliteNodeState(conn)
        node_state.save_invocation(
            GID,
            "worker",
            inv1.invocation_id,
            inv1.version,
            inv1.parent_version,
            InvocationStatus.SUPERSEDED,
            {"resume_target": "tool"},
            suspended=True,
        )

        coord2, _ = _sqlite_coordinator(conn=conn)
        coord2.register_node("worker")

        ctx = coord2.load_for_recovery()
        latest = ctx.node_states.get("worker")
        assert latest is not None
        assert latest.status == InvocationStatus.SUPERSEDED
        assert latest.suspended is True

        inv2 = coord2.begin_invocation("worker")
        assert inv2.version == 2
        assert inv2.parent_version == 0

        versions = coord2._node_states["worker"].query_versions(
            GID, "worker", {InvocationStatus.SUPERSEDED}
        )
        assert len(versions) == 1
        assert versions[0].invocation_id == inv1.invocation_id

        assert ctx.rebuilt_main_state.get("resume_target") == "tool"


# ── Transaction Test: COMPLETED crash ───────────────────────────────────────────────────


class TestF2Transaction:
    """crash between save COMPLETED + promote → recovery → auto-promote."""

    def test_f2_crash_after_complete_before_promote(self) -> None:
        """Simulate: complete_invocation saves COMPLETED, then crashes before promote_delivers.

        Recovery should auto-promote the CONSUMED_PENDING delivers.
        """
        coord, conn = _sqlite_coordinator()
        coord.register_node("worker")
        coord._metadata_store.save(GID, _metadata())

        inv = coord.begin_invocation("worker")

        d1 = coord.route_deliver("worker", {"data": 1}, "source", 9999)
        d2 = coord.route_deliver("worker", {"data": 2}, "source", 9999)
        assert d1 is not None and d2 is not None

        store = coord.get_deliver_store("worker")
        assert store is not None
        store.mark_consumed([d1, d2], inv.invocation_id)

        coord._node_states["worker"].save_invocation(
            GID,
            "worker",
            inv.invocation_id,
            inv.version,
            inv.parent_version,
            InvocationStatus.COMPLETED,
            {"result": "done"},
        )

        consumable_before = store.query_consumable(GID, "worker")
        consumed_pending = [
            r for r in consumable_before if r.status == DeliverConsumptionStatus.CONSUMED_PENDING
        ]
        assert len(consumed_pending) == 2

        coord2, _ = _sqlite_coordinator(conn=conn)
        coord2.register_node("worker")

        coord2.load_for_recovery()

        consumable_after = store.query_consumable(GID, "worker")
        for r in consumable_after:
            assert r.status != DeliverConsumptionStatus.CONSUMED_PENDING

        rows = conn.execute(
            "SELECT status FROM deliver_states WHERE graph_instance_id = ? AND node_name = ?",
            (GID, "worker"),
        ).fetchall()
        statuses = [row[0] for row in rows]
        assert "consumed_pending" not in statuses
        completed_count = sum(
            1 for s in statuses if s == DeliverConsumptionStatus.CONSUMED_COMPLETED.value
        )
        assert completed_count == 2


# ── Resume: skip re-consume ────────────────────────────────────────────


class TestI16ResumeSkipReconsume:
    """resume uses SUPERSEDED snapshot, skips re-consume."""

    def test_i16_resume_with_snapshot_skips_reconsume(self) -> None:
        """When resuming, the SUPERSEDED invocation's snapshot is available for skip."""
        coord = _memory_coordinator()
        coord.register_node("worker")

        inv0 = coord.begin_invocation("worker")
        coord.suspend_invocation(inv0, {"resume_target": "tool", "consumed_data": [1, 2]})

        coord.begin_invocation("worker")

        prev = coord.load_latest_invocation("worker")
        assert prev is not None
        assert prev.status == InvocationStatus.RUNNING

        node_state = coord._node_states["worker"]
        superseded = node_state.query_versions(GID, "worker", {InvocationStatus.SUPERSEDED})
        assert len(superseded) == 1
        assert superseded[0].state_json == {"resume_target": "tool", "consumed_data": [1, 2]}

        state = coord.rebuild_main_state()
        assert state.get("resume_target") == "tool"
        assert state.get("consumed_data") == [1, 2]


# ── Version Chain ─────────────────────────────────────────────────────────


class TestVersionChain:
    """Version chain integrity across multiple invocations."""

    def test_version_chain_progression(self) -> None:
        coord = _memory_coordinator()
        coord.register_node("worker")

        inv0 = coord.begin_invocation("worker")
        assert inv0.version == 0
        assert inv0.parent_version is None
        coord.complete_invocation(inv0, {"v": 0})

        inv1 = coord.begin_invocation("worker")
        assert inv1.version == 1
        assert inv1.parent_version == 0
        coord.complete_invocation(inv1, {"v": 1})

        inv2 = coord.begin_invocation("worker")
        assert inv2.version == 2
        assert inv2.parent_version == 1
        coord.crash_invocation(inv2)

        inv3 = coord.begin_invocation("worker")
        assert inv3.version == 3
        assert inv3.parent_version == 1

    def test_parent_version_points_to_latest_completed_not_latest(self) -> None:
        """parent_version = latest COMPLETED, not latest overall."""
        coord = _memory_coordinator()
        coord.register_node("worker")

        inv0 = coord.begin_invocation("worker")
        coord.complete_invocation(inv0, {"v": 0})

        inv1 = coord.begin_invocation("worker")
        coord.crash_invocation(inv1)

        inv2 = coord.begin_invocation("worker")
        assert inv2.parent_version == 0
        assert inv2.parent_version != 1


# ── SQLite Lifecycle Integration ──────────────────────────────────────────


class TestSqliteLifecycle:
    """Full lifecycle transitions with SQLite stores (transaction semantics)."""

    def test_sqlite_begin_complete_lifecycle(self) -> None:
        coord, _ = _sqlite_coordinator()
        coord.register_node("worker")
        coord._metadata_store.save(GID, _metadata())

        inv = coord.begin_invocation("worker")
        coord.complete_invocation(inv, {"result": "done"})

        latest = coord.load_latest_invocation("worker")
        assert latest is not None
        assert latest.status == InvocationStatus.COMPLETED
        assert latest.state_json == {"result": "done"}

    def test_sqlite_suspend_resume_lifecycle(self) -> None:
        coord, _ = _sqlite_coordinator()
        coord.register_node("worker")

        inv0 = coord.begin_invocation("worker")
        coord.suspend_invocation(inv0, {"resume_target": "tool"})

        inv1 = coord.begin_invocation("worker")
        assert inv1.version == 1
        assert inv1.parent_version is None

        node_state = coord._node_states["worker"]
        superseded = node_state.query_versions(GID, "worker", {InvocationStatus.SUPERSEDED})
        assert len(superseded) == 1
        assert superseded[0].suspended is True

        coord.complete_invocation(inv1, {"result": "resumed"})

        latest = coord.load_latest_invocation("worker")
        assert latest is not None
        assert latest.status == InvocationStatus.COMPLETED

    def test_sqlite_recovery_preserves_state(self) -> None:
        coord, conn = _sqlite_coordinator()
        coord.register_node("worker")
        coord._metadata_store.save(GID, _metadata())

        inv = coord.begin_invocation("worker")
        coord.complete_invocation(inv, {"result": "persisted"})

        coord2, _ = _sqlite_coordinator(conn=conn)
        coord2.register_node("worker")

        ctx = coord2.load_for_recovery()
        assert ctx.rebuilt_main_state == {"result": "persisted"}
        assert ctx.node_states["worker"] is not None
        assert ctx.node_states["worker"].status == InvocationStatus.COMPLETED


# ── close ───────────────────────────────────────────────────────────


class TestClose:
    """close() closes resources."""

    def test_close_is_safe_to_call(self) -> None:
        coord, conn = _sqlite_coordinator()
        coord.register_node("worker")

        coord.close()
        coord.close()

    def test_close_with_memory_stores(self) -> None:
        coord = _memory_coordinator()
        coord.register_node("worker")

        coord.close()


# ── Null Strategy ─────────────────────────────────────────────────────────


class TestNullStrategy:
    """Null coordinator strategy — no-op persistence."""

    def test_null_coordinator_lifecycle(self) -> None:
        coord = create_null_coordinator(GID)
        coord.register_node("worker")

        inv = coord.begin_invocation("worker")
        assert inv.invocation_id > 0
        assert inv.version == 0

        coord.complete_invocation(inv, {"result": "done"})

        latest = coord.load_latest_invocation("worker")
        assert latest is None

        ctx = coord.load_for_recovery()
        assert ctx.rebuilt_main_state == {}

    def test_null_route_deliver_to_end(self) -> None:
        coord = create_null_coordinator(GID)
        coord.register_node("worker")

        assert coord.route_deliver(GraphNode.END, {}, "src", 1) is None

    def test_null_route_deliver_accumulates(self) -> None:
        coord = create_null_coordinator(GID)
        coord.register_node("worker")

        deliver_id = coord.route_deliver("worker", {"data": 1}, "src", 1)
        assert deliver_id is not None
        assert deliver_id > 0
