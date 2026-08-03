"""Tests for ``create_null_coordinator`` — Null coordinator behavior.

Verifies the formalized NullCoordinator factory and its behavior:

- Factory returns a ``GraphPersistenceCoordinator`` wired with Null stores.
- Lifecycle methods (begin/complete/cancel/crash/suspend/finalize) are no-op
  or in-memory — ``NullNodeState`` discards all saves.
- ``load_for_recovery`` returns a fresh ``RecoveryContext`` (empty state).
- Deliver-only routing through ``NullDeliverStore`` in-memory queue:
  accumulate creates PENDING records, mark_consumed REMOVES them (no CONSUMED
  state), promote_consumed is a no-op.
- Suspend/resume: ``suspend_invocation`` is a no-op; the coordinator holds no
  state across suspend/resume — AgentContext is the orthogonal turn-state layer.
"""

from __future__ import annotations

import pytest

from modex_graph import (
    DeliverConsumptionStatus,
    GraphNode,
    GraphPersistenceCoordinator,
    RecoveryContext,
    RoutingError,
    create_null_coordinator,
)


class TestCreateNullCoordinator:
    """Factory function ``create_null_coordinator``."""

    def test_returns_graph_persistence_coordinator(self) -> None:
        coord = create_null_coordinator()
        assert isinstance(coord, GraphPersistenceCoordinator)

    def test_default_graph_instance_id_is_zero(self) -> None:
        coord = create_null_coordinator()
        coord.register_node("llm")
        deliver_id = coord.route_deliver("llm", {"data": 1}, "start", 1)
        assert deliver_id is not None
        records = coord.collect_consumable_delivers("llm", 1)
        assert len(records) == 1
        assert records[0].graph_instance_id == 0

    def test_custom_graph_instance_id(self) -> None:
        coord = create_null_coordinator(graph_instance_id=42)
        coord.register_node("llm")
        deliver_id = coord.route_deliver("llm", {}, "start", 1)
        assert deliver_id is not None
        records = coord.collect_consumable_delivers("llm", 1)
        assert records[0].graph_instance_id == 42


class TestNullLifecycleNoOp:
    """Null coordinator lifecycle — begin returns context, rest are no-op."""

    def test_begin_invocation_returns_valid_context(self) -> None:
        coord = create_null_coordinator()
        coord.register_node("llm")
        inv = coord.begin_invocation("llm")
        assert inv.invocation_id > 0
        assert inv.node_name == "llm"
        assert inv.version == 0
        assert inv.parent_version is None

    def test_complete_invocation_is_noop(self) -> None:
        coord = create_null_coordinator()
        coord.register_node("llm")
        inv = coord.begin_invocation("llm")
        coord.complete_invocation(inv, {"result": "done"})
        assert coord.load_latest_invocation("llm") is None

    def test_cancel_invocation_is_noop(self) -> None:
        coord = create_null_coordinator()
        coord.register_node("llm")
        inv = coord.begin_invocation("llm")
        coord.cancel_invocation(inv)
        assert coord.load_latest_invocation("llm") is None

    def test_crash_invocation_is_noop(self) -> None:
        coord = create_null_coordinator()
        coord.register_node("llm")
        inv = coord.begin_invocation("llm")
        coord.crash_invocation(inv)
        assert coord.load_latest_invocation("llm") is None

    def test_suspend_invocation_is_noop(self) -> None:
        coord = create_null_coordinator()
        coord.register_node("llm")
        inv = coord.begin_invocation("llm")
        coord.suspend_invocation(inv, {"resume_target": "tool"})
        assert coord.load_latest_invocation("llm") is None

    def test_finalize_invocation_is_noop(self) -> None:
        coord = create_null_coordinator()
        coord.register_node("llm")
        inv = coord.begin_invocation("llm")
        coord.finalize_invocation(inv)

    def test_begin_invocation_unregistered_node_raises(self) -> None:
        coord = create_null_coordinator()
        with pytest.raises(RoutingError):
            coord.begin_invocation("nonexistent")

    def test_repeated_begin_always_returns_version_zero(self) -> None:
        """NullNodeState has no memory — every begin_invocation starts fresh."""
        coord = create_null_coordinator()
        coord.register_node("llm")
        for _ in range(3):
            inv = coord.begin_invocation("llm")
            assert inv.version == 0
            assert inv.parent_version is None


class TestNullRecovery:
    """Null coordinator recovery — returns fresh empty context."""

    def test_load_for_recovery_returns_empty_state(self) -> None:
        coord = create_null_coordinator()
        coord.register_node("llm")
        coord.register_node("tool")
        ctx = coord.load_for_recovery()
        assert isinstance(ctx, RecoveryContext)
        assert ctx.rebuilt_main_state == {}
        assert ctx.node_states["llm"] is None
        assert ctx.node_states["tool"] is None

    def test_load_for_recovery_after_complete_still_empty(self) -> None:
        coord = create_null_coordinator()
        coord.register_node("llm")
        inv = coord.begin_invocation("llm")
        coord.complete_invocation(inv, {"result": "done"})
        ctx = coord.load_for_recovery()
        assert ctx.rebuilt_main_state == {}
        assert ctx.node_states["llm"] is None

    def test_get_graph_state_returns_empty_node_lists(self) -> None:
        coord = create_null_coordinator()
        coord.register_node("llm")
        snapshot = coord.get_graph_state()
        assert snapshot.metadata.graph_instance_id == 0
        assert snapshot.nodes["llm"] == []


class TestNullDeliverOnlyRouting:
    """Deliver-only routing through NullDeliverStore in-memory queue.

    NullDeliverStore semantics: accumulate creates PENDING records, mark_consumed
    REMOVES records from the queue (no CONSUMED state), promote_consumed is a
    no-op. This is the path React 4 nodes (START/LLM/TOOL/END) use via
    Node.deliver() -> coordinator.route_deliver().
    """

    def test_route_deliver_to_end_returns_none(self) -> None:
        coord = create_null_coordinator()
        coord.register_node("llm")
        assert coord.route_deliver(GraphNode.END, {"data": 1}, "llm", 1) is None

    def test_route_deliver_to_unregistered_node_raises(self) -> None:
        coord = create_null_coordinator()
        with pytest.raises(RoutingError):
            coord.route_deliver("nonexistent", {}, "llm", 1)

    def test_route_deliver_accumulates_and_returns_id(self) -> None:
        coord = create_null_coordinator()
        coord.register_node("tool")
        deliver_id = coord.route_deliver("tool", {"args": [1]}, "llm", 100)
        assert deliver_id is not None
        assert deliver_id > 0

    def test_collect_consumable_returns_accumulated_records(self) -> None:
        coord = create_null_coordinator()
        coord.register_node("tool")
        coord.route_deliver("tool", {"args": 1}, "llm", 100)
        coord.route_deliver("tool", {"args": 2}, "llm", 101)
        records = coord.collect_consumable_delivers("tool", 200)
        assert len(records) == 2
        assert records[0].content == {"args": 1}
        assert records[1].content == {"args": 2}
        assert records[0].source_node == "llm"
        assert records[0].source_invocation_id == 100
        assert records[0].status == DeliverConsumptionStatus.PENDING

    def test_full_deliver_cycle_accumulate_mark_promote(self) -> None:
        """Full deliver-only routing cycle: accumulate -> query -> mark -> promote.

        After mark_consumed (removes) + promote_delivers (no-op), queue is empty.
        """
        coord = create_null_coordinator()
        coord.register_node("tool")

        d1 = coord.route_deliver("tool", {"x": 1}, "llm", 100)
        d2 = coord.route_deliver("tool", {"x": 2}, "llm", 100)
        assert d1 is not None and d2 is not None

        records = coord.collect_consumable_delivers("tool", 200)
        assert len(records) == 2

        coord.mark_delivers_consumed("tool", [d1, d2], 200)
        assert len(coord.collect_consumable_delivers("tool", 200)) == 0

        coord.promote_delivers("tool", 200)
        assert len(coord.collect_consumable_delivers("tool", 200)) == 0

    def test_partial_mark_consumed_leaves_remaining(self) -> None:
        coord = create_null_coordinator()
        coord.register_node("tool")
        d1 = coord.route_deliver("tool", {"x": 1}, "llm", 100)
        d2 = coord.route_deliver("tool", {"x": 2}, "llm", 100)
        assert d1 is not None and d2 is not None

        coord.mark_delivers_consumed("tool", [d1], 200)
        remaining = coord.collect_consumable_delivers("tool", 200)
        assert len(remaining) == 1
        assert remaining[0].deliver_id == d2

    def test_delivers_isolated_per_node(self) -> None:
        coord = create_null_coordinator()
        coord.register_node("llm")
        coord.register_node("tool")
        coord.route_deliver("llm", {"from": "tool"}, "tool", 100)
        coord.route_deliver("tool", {"from": "llm"}, "llm", 200)

        llm_records = coord.collect_consumable_delivers("llm", 300)
        tool_records = coord.collect_consumable_delivers("tool", 300)
        assert len(llm_records) == 1
        assert len(tool_records) == 1
        assert llm_records[0].content == {"from": "tool"}
        assert tool_records[0].content == {"from": "llm"}

    def test_react_four_nodes_all_registered_and_routable(self) -> None:
        """React 4 nodes (start/llm/tool/end) can all be registered and routed."""
        coord = create_null_coordinator()
        for name in ("start", "llm", "tool", "end"):
            coord.register_node(name)

        # Route from start -> llm, llm -> tool, tool -> llm.
        d1 = coord.route_deliver("llm", None, "start", 1)
        d2 = coord.route_deliver("tool", {"result": "ok"}, "llm", 2)
        assert d1 is not None and d2 is not None

        # END routing returns None (no deliver_store for END sentinel).
        assert coord.route_deliver(GraphNode.END, {}, "tool", 3) is None

        # Each node's queue is independent.
        assert len(coord.collect_consumable_delivers("llm", 0)) == 1
        assert len(coord.collect_consumable_delivers("tool", 0)) == 1


class TestNullSuspendResume:
    """Suspend/resume with Null coordinator — coordinator is no-op, AgentContext holds state.

    suspend_invocation is a no-op (NullNodeState discards the snapshot). On
    resume, begin_invocation starts fresh (version=0, parent_version=None)
    because NullNodeState has no memory. The deliver queue (NullDeliverStore
    in-memory) persists across suspend/resume since the same store instance
    is reused.
    """

    def test_suspend_does_not_persist_state(self) -> None:
        coord = create_null_coordinator()
        coord.register_node("tool")
        inv = coord.begin_invocation("tool")
        coord.suspend_invocation(inv, {"resume_target": "tool", "batch_id": "abc"})
        assert coord.load_latest_invocation("tool") is None

    def test_resume_starts_fresh(self) -> None:
        coord = create_null_coordinator()
        coord.register_node("tool")

        inv1 = coord.begin_invocation("tool")
        coord.suspend_invocation(inv1, {"resume_target": "tool"})

        inv2 = coord.begin_invocation("tool")
        assert inv2.invocation_id > 0
        assert inv2.version == 0
        assert inv2.parent_version is None
        assert inv2.invocation_id != inv1.invocation_id

    def test_deliver_queue_survives_suspend_resume(self) -> None:
        """NullDeliverStore is in-memory on the store instance — persists across suspend/resume."""
        coord = create_null_coordinator()
        coord.register_node("tool")

        d1 = coord.route_deliver("tool", {"step": 1}, "llm", 100)
        assert d1 is not None

        inv = coord.begin_invocation("tool")
        coord.suspend_invocation(inv, {"resume_target": "tool"})

        records = coord.collect_consumable_delivers("tool", 200)
        assert len(records) == 1
        assert records[0].deliver_id == d1

    def test_full_suspend_resume_with_deliver_consumption(self) -> None:
        """Full cycle: accumulate -> suspend -> resume -> consume -> complete."""
        coord = create_null_coordinator()
        coord.register_node("tool")

        d1 = coord.route_deliver("tool", {"x": 1}, "llm", 100)
        assert d1 is not None

        inv1 = coord.begin_invocation("tool")
        coord.suspend_invocation(inv1, {"resume_target": "tool"})

        inv2 = coord.begin_invocation("tool")
        records = coord.collect_consumable_delivers("tool", inv2.invocation_id)
        assert len(records) == 1

        coord.mark_delivers_consumed("tool", [d1], inv2.invocation_id)
        coord.complete_invocation(inv2, {"result": "done"})

        assert len(coord.collect_consumable_delivers("tool", 0)) == 0
