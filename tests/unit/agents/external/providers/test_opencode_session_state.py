"""Unit tests for ``OpenCodeSessionState`` — shared session-state registry.

Tests cover the data model (``SessionActivity``/``SessionNode``/``TurnState``),
event-driven state transitions, parentID tree growth, subtree queries
(``subtree_ids``/``all_idle``/``last_event_ms``), waiter notification,
REST rebuild (success / fetch failure / root 404), LRU cleanup, and
reconnect-pending signaling.

All tests use mock waiters and mock clients — no real HTTP or SSE.
"""

# ruff: noqa: ANN401

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from modex_agent.agents.external.providers.opencode.session_state import (
    OpenCodeSessionState,
    SessionActivity,
    SessionNode,
    TurnState,
)
from modex_agent.agents.external.providers.opencode.v2_client import (
    OpencodeV2Client,
    OpencodeV2Error,
)

_DIR = "/tmp/test"


# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------


class _MockWaiter:
    """Test double for ``TurnCompletionWaiter`` (forward reference).

    The real waiter is implemented in ``turn_waiter.py`` (next task).
    The registry only depends on ``root_sid`` and ``touch()``.
    """

    def __init__(self, root_sid: str) -> None:
        self.root_sid = root_sid
        self.touch_count = 0

    def touch(self) -> None:
        self.touch_count += 1


def _make_client(
    *,
    children: dict[str, list[dict[str, Any]]] | None = None,
    status: dict[str, str] | None = None,
    children_error: dict[str, Exception] | None = None,
    status_error: dict[str, Exception] | None = None,
) -> AsyncMock:
    """Build a mock ``OpencodeV2Client`` with ``get_children`` + ``get_session_status_v1``."""
    client = AsyncMock(spec=OpencodeV2Client)
    _children = children or {}
    _status = status or {}
    _children_error = children_error or {}
    _status_error = status_error or {}

    async def _get_children(session_id: str, *, directory: str | None = None) -> list[dict[str, Any]]:
        if session_id in _children_error:
            raise _children_error[session_id]
        return _children.get(session_id, [])

    async def _get_status(session_id: str, *, directory: str | None = None) -> str:
        if session_id in _status_error:
            raise _status_error[session_id]
        return _status.get(session_id, "idle")

    client.get_children = _get_children
    client.get_session_status_v1 = _get_status
    return client


# ---------------------------------------------------------------------------
# 1.1 — single session activity transitions
# ---------------------------------------------------------------------------


class TestSingleSessionActivity:
    def test_busy_then_idle_transitions(self) -> None:
        state = OpenCodeSessionState()
        state.register_waiter(_MockWaiter("root"))

        state.on_event("root", "session.status", activity=SessionActivity.BUSY)
        assert state._nodes["root"].activity is SessionActivity.BUSY

        state.on_event("root", "session.status", activity=SessionActivity.IDLE)
        assert state._nodes["root"].activity is SessionActivity.IDLE

    def test_session_idle_deprecated_treated_as_idle(self) -> None:
        state = OpenCodeSessionState()
        state.register_waiter(_MockWaiter("root"))

        state.on_event("root", "session.status", activity=SessionActivity.BUSY)
        state.on_event("root", "session.idle", activity=SessionActivity.IDLE)

        assert state._nodes["root"].activity is SessionActivity.IDLE

    def test_session_error_sets_error_activity(self) -> None:
        state = OpenCodeSessionState()
        state.register_waiter(_MockWaiter("root"))

        state.on_event("root", "session.error", activity=SessionActivity.ERROR)

        assert state._nodes["root"].activity is SessionActivity.ERROR


# ---------------------------------------------------------------------------
# 1.2 — subtree_ids includes child after session.created
# ---------------------------------------------------------------------------


class TestSubtreeIds:
    def test_child_added_to_subtree_on_session_created(self) -> None:
        state = OpenCodeSessionState()
        state.register_waiter(_MockWaiter("root"))

        state.on_event("root", "session.status", activity=SessionActivity.BUSY)
        state.on_event("child", "session.created", parent_sid="root")

        assert "child" in state.subtree_ids("root")
        assert state._nodes["child"].parent_sid == "root"


# ---------------------------------------------------------------------------
# 1.3 — nested child → grandchild
# ---------------------------------------------------------------------------


class TestNestedSubtree:
    def test_grandchild_in_subtree(self) -> None:
        state = OpenCodeSessionState()
        state.register_waiter(_MockWaiter("root"))

        state.on_event("root", "session.status", activity=SessionActivity.BUSY)
        state.on_event("child", "session.created", parent_sid="root")
        state.on_event("grandchild", "session.created", parent_sid="child")

        subtree = state.subtree_ids("root")
        assert "root" in subtree
        assert "child" in subtree
        assert "grandchild" in subtree


# ---------------------------------------------------------------------------
# 1.4 — unrelated session (parent not in tree) ignored
# ---------------------------------------------------------------------------


class TestUnrelatedSession:
    def test_unrelated_session_created_ignored(self) -> None:
        state = OpenCodeSessionState()
        state.register_waiter(_MockWaiter("root"))

        state.on_event("root", "session.status", activity=SessionActivity.BUSY)
        state.on_event("unrelated", "session.created", parent_sid="other")

        assert "unrelated" not in state.subtree_ids("root")
        assert "unrelated" not in state._nodes

    def test_status_event_for_non_tree_session_ignored(self) -> None:
        state = OpenCodeSessionState()
        state.register_waiter(_MockWaiter("root"))

        state.on_event("root", "session.status", activity=SessionActivity.BUSY)
        state.on_event("stranger", "session.status", activity=SessionActivity.BUSY)

        assert "stranger" not in state._nodes


# ---------------------------------------------------------------------------
# 1.5 + 1.11 — all_idle
# ---------------------------------------------------------------------------


class TestAllIdle:
    def test_one_busy_one_idle_returns_false(self) -> None:
        state = OpenCodeSessionState()
        state.register_waiter(_MockWaiter("root"))

        state.on_event("root", "session.status", activity=SessionActivity.IDLE)
        state.on_event("child", "session.created", parent_sid="root")
        state.on_event("child", "session.status", activity=SessionActivity.BUSY)

        assert state.all_idle("root") is False

    def test_all_idle_returns_true(self) -> None:
        state = OpenCodeSessionState()
        state.register_waiter(_MockWaiter("root"))

        state.on_event("root", "session.status", activity=SessionActivity.IDLE)
        state.on_event("child", "session.created", parent_sid="root")
        state.on_event("child", "session.status", activity=SessionActivity.IDLE)

        assert state.all_idle("root") is True

    def test_root_not_in_nodes_returns_false(self) -> None:
        """Empty tree (root not yet seen) → False, not True (design 5.3 _recheck)."""
        state = OpenCodeSessionState()
        assert state.all_idle("nonexistent") is False

    def test_error_child_counts_as_idle(self) -> None:
        """ERROR is a converged state — does not block all_idle (design 5.1)."""
        state = OpenCodeSessionState()
        state.register_waiter(_MockWaiter("root"))

        state.on_event("root", "session.status", activity=SessionActivity.IDLE)
        state.on_event("child", "session.created", parent_sid="root")
        state.on_event("child", "session.status", activity=SessionActivity.ERROR)

        assert state.all_idle("root") is True

    def test_busy_child_blocks_all_idle(self) -> None:
        state = OpenCodeSessionState()
        state.register_waiter(_MockWaiter("root"))

        state.on_event("root", "session.status", activity=SessionActivity.IDLE)
        state.on_event("child", "session.created", parent_sid="root")
        state.on_event("child", "session.status", activity=SessionActivity.BUSY)

        assert state.all_idle("root") is False

    def test_root_busy_blocks_all_idle(self) -> None:
        state = OpenCodeSessionState()
        state.register_waiter(_MockWaiter("root"))

        state.on_event("root", "session.status", activity=SessionActivity.BUSY)

        assert state.all_idle("root") is False


# ---------------------------------------------------------------------------
# 1.6 — waiter registered → on_event subtree event → waiter.touch() called
# ---------------------------------------------------------------------------


class TestWaiterNotification:
    def test_waiter_touched_on_root_status_event(self) -> None:
        state = OpenCodeSessionState()
        waiter = _MockWaiter("root")
        state.register_waiter(waiter)

        state.on_event("root", "session.status", activity=SessionActivity.BUSY)
        assert waiter.touch_count >= 1

    def test_waiter_touched_on_child_event(self) -> None:
        state = OpenCodeSessionState()
        waiter = _MockWaiter("root")
        state.register_waiter(waiter)

        state.on_event("root", "session.status", activity=SessionActivity.BUSY)
        state.on_event("child", "session.created", parent_sid="root")
        initial = waiter.touch_count

        state.on_event("child", "session.status", activity=SessionActivity.IDLE)

        assert waiter.touch_count > initial

    def test_waiter_not_touched_on_unrelated_event(self) -> None:
        state = OpenCodeSessionState()
        waiter = _MockWaiter("root")
        state.register_waiter(waiter)

        state.on_event("root", "session.status", activity=SessionActivity.BUSY)
        initial = waiter.touch_count

        state.on_event("stranger", "session.status", activity=SessionActivity.BUSY)

        assert waiter.touch_count == initial

    def test_unregister_waiter_stops_notification(self) -> None:
        state = OpenCodeSessionState()
        waiter = _MockWaiter("root")
        state.register_waiter(waiter)

        state.on_event("root", "session.status", activity=SessionActivity.BUSY)
        state.unregister_waiter(waiter)
        initial = waiter.touch_count

        state.on_event("root", "session.status", activity=SessionActivity.IDLE)

        assert waiter.touch_count == initial


# ---------------------------------------------------------------------------
# 1.7 — rebuild_subtree (mock client returning children + status)
# ---------------------------------------------------------------------------


class TestRebuildSubtree:
    async def test_rebuild_success_rebuilds_tree_and_state(self) -> None:
        state = OpenCodeSessionState()
        state.register_waiter(_MockWaiter("root"))

        client = _make_client(
            children={
                "root": [{"id": "child1", "parentID": "root"}],
                "child1": [],
            },
            status={"root": "busy", "child1": "idle"},
        )

        await state.rebuild_subtree("root", client, directory=_DIR)

        assert "root" in state._nodes
        assert state._nodes["root"].activity is SessionActivity.BUSY
        assert state._nodes["root"].discovered is True
        assert "child1" in state._nodes
        assert state._nodes["child1"].activity is SessionActivity.IDLE
        assert state._nodes["child1"].parent_sid == "root"
        assert state._nodes["child1"].discovered is True
        assert state.is_reconnect_pending() is False
        assert state.is_rebuild_pending() is False

    async def test_rebuild_nested_children(self) -> None:
        state = OpenCodeSessionState()
        state.register_waiter(_MockWaiter("root"))

        client = _make_client(
            children={
                "root": [{"id": "c1", "parentID": "root"}],
                "c1": [{"id": "gc1", "parentID": "c1"}],
                "gc1": [],
            },
            status={"root": "idle", "c1": "busy", "gc1": "idle"},
        )

        await state.rebuild_subtree("root", client, directory=_DIR)

        subtree = state.subtree_ids("root")
        assert {"root", "c1", "gc1"} <= subtree
        assert state._nodes["c1"].activity is SessionActivity.BUSY
        assert state._nodes["gc1"].parent_sid == "c1"

    async def test_rebuild_fetch_failure_sets_rebuild_pending(self) -> None:
        """fetch error → rebuild_pending=True, NOT fake idle (design 5.7)."""
        state = OpenCodeSessionState()
        state.register_waiter(_MockWaiter("root"))

        error = OpencodeV2Error(
            tag="UnknownError", message="fetch failed", status=500, body=None
        )
        client = _make_client(children_error={"root": error})

        await state.rebuild_subtree("root", client, directory=_DIR)

        assert state.is_rebuild_pending() is True
        # Tree must NOT be faked as idle — root not in nodes → all_idle False
        assert state.all_idle("root") is False

    async def test_rebuild_failure_preserves_existing_state(self) -> None:
        """If tree already had state, rebuild failure does not erase it."""
        state = OpenCodeSessionState()
        state.register_waiter(_MockWaiter("root"))

        # Pre-populate via events
        state.on_event("root", "session.status", activity=SessionActivity.BUSY)
        state.on_event("child", "session.created", parent_sid="root")

        error = OpencodeV2Error(
            tag="UnknownError", message="fetch failed", status=500, body=None
        )
        client = _make_client(children_error={"root": error})

        await state.rebuild_subtree("root", client, directory=_DIR)

        assert state.is_rebuild_pending() is True
        # Existing BUSY state preserved — not faked as idle
        assert state._nodes["root"].activity is SessionActivity.BUSY
        assert state.all_idle("root") is False

    async def test_rebuild_clears_reconnect_pending_on_success(self) -> None:
        state = OpenCodeSessionState()
        state.register_waiter(_MockWaiter("root"))
        state.mark_reconnect_pending()
        assert state.is_reconnect_pending() is True

        client = _make_client(
            children={"root": [], "gc1": []},
            status={"root": "idle"},
        )

        await state.rebuild_subtree("root", client, directory=_DIR)

        assert state.is_reconnect_pending() is False


# ---------------------------------------------------------------------------
# 1.8 — LRU cleanup
# ---------------------------------------------------------------------------


class TestLruCleanup:
    def test_old_sid_outside_subtree_cleaned(self) -> None:
        state = OpenCodeSessionState()
        state.register_waiter(_MockWaiter("root"))

        state.on_event("root", "session.status", activity=SessionActivity.IDLE)

        # Add a stale node NOT in any waiter's subtree
        state._nodes["stale"] = SessionNode(
            sid="stale",
            parent_sid=None,
            activity=SessionActivity.IDLE,
            last_event_ms=100,
        )

        state.lru_cleanup(threshold_ns=1000)

        assert "stale" not in state._nodes
        assert "root" in state._nodes

    def test_active_waiter_subtree_retained(self) -> None:
        state = OpenCodeSessionState()
        state.register_waiter(_MockWaiter("root"))

        state.on_event("root", "session.status", activity=SessionActivity.IDLE)
        state.on_event("child", "session.created", parent_sid="root")

        # Make both old
        state._nodes["root"].last_event_ms = 100
        state._nodes["child"].last_event_ms = 100

        state.lru_cleanup(threshold_ns=1000)

        # Both retained — in active waiter's subtree
        assert "root" in state._nodes
        assert "child" in state._nodes

    def test_recent_sid_retained(self) -> None:
        import time

        state = OpenCodeSessionState()
        state.register_waiter(_MockWaiter("root"))

        state.on_event("root", "session.status", activity=SessionActivity.IDLE)

        # Add a stale node outside subtree
        state._nodes["stale"] = SessionNode(
            sid="stale",
            parent_sid=None,
            activity=SessionActivity.IDLE,
            last_event_ms=time.monotonic_ns(),
        )

        state.lru_cleanup(threshold_ns=1)

        # Recent → retained
        assert "stale" in state._nodes


# ---------------------------------------------------------------------------
# 1.9 — mark_reconnect_pending touches all waiters
# ---------------------------------------------------------------------------


class TestReconnectPending:
    def test_mark_reconnect_pending_touches_all_waiters(self) -> None:
        state = OpenCodeSessionState()
        w1 = _MockWaiter("root1")
        w2 = _MockWaiter("root2")
        state.register_waiter(w1)
        state.register_waiter(w2)

        before1 = w1.touch_count
        before2 = w2.touch_count

        state.mark_reconnect_pending()

        assert state.is_reconnect_pending() is True
        assert w1.touch_count > before1
        assert w2.touch_count > before2

    def test_clear_reconnect_pending(self) -> None:
        state = OpenCodeSessionState()
        state.mark_reconnect_pending()
        assert state.is_reconnect_pending() is True

        state.clear_reconnect_pending()
        assert state.is_reconnect_pending() is False


# ---------------------------------------------------------------------------
# 1.10 — rebuild_subtree root 404 → mark_root_missing
# ---------------------------------------------------------------------------


class TestRootMissing:
    async def test_rebuild_root_404_marks_root_missing(self) -> None:
        state = OpenCodeSessionState()
        waiter = _MockWaiter("root")
        state.register_waiter(waiter)

        error = OpencodeV2Error(
            tag="SessionNotFoundError",
            message="not found",
            status=404,
            body=None,
        )
        client = _make_client(children_error={"root": error})

        await state.rebuild_subtree("root", client, directory=_DIR)

        assert state.is_root_missing("root") is True
        # Waiter should be touched so it re-checks and sees root_missing
        assert waiter.touch_count > 0

    async def test_root_missing_does_not_fake_idle(self) -> None:
        state = OpenCodeSessionState()
        state.register_waiter(_MockWaiter("root"))

        error = OpencodeV2Error(
            tag="SessionNotFoundError",
            message="not found",
            status=404,
            body=None,
        )
        client = _make_client(children_error={"root": error})

        await state.rebuild_subtree("root", client, directory=_DIR)

        assert state.is_root_missing("root") is True
        # Tree is empty → all_idle False (not faked)
        assert state.all_idle("root") is False


# ---------------------------------------------------------------------------
# last_event_ms query
# ---------------------------------------------------------------------------


class TestLastEventMs:
    def test_max_last_event_ms_across_subtree(self) -> None:
        state = OpenCodeSessionState()
        state.register_waiter(_MockWaiter("root"))

        state.on_event("root", "session.status", activity=SessionActivity.BUSY)
        root_ms = state._nodes["root"].last_event_ms

        state.on_event("child", "session.created", parent_sid="root")
        child_ms = state._nodes["child"].last_event_ms

        assert state.last_event_ms("root") == max(root_ms, child_ms)

    def test_empty_tree_returns_none(self) -> None:
        state = OpenCodeSessionState()
        assert state.last_event_ms("nonexistent") is None


# ---------------------------------------------------------------------------
# SessionNode / TurnState model basics
# ---------------------------------------------------------------------------


class TestModels:
    def test_session_node_defaults(self) -> None:
        node = SessionNode(sid="s1")
        assert node.sid == "s1"
        assert node.parent_sid is None
        assert node.activity is SessionActivity.IDLE
        assert node.discovered is False
        assert node.last_event_ms > 0

    def test_session_node_activity_mutation(self) -> None:
        """frozen=False — state needs updating (design 5.1)."""
        node = SessionNode(sid="s1")
        node.activity = SessionActivity.BUSY
        assert node.activity is SessionActivity.BUSY

    def test_session_node_extra_forbidden(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            SessionNode(sid="s1", unknown_field="x")  # type: ignore[call-arg]

    def test_turn_state_values(self) -> None:
        assert TurnState.ACTIVE == "active"
        assert TurnState.QUIESCING == "quiescing"
        assert TurnState.COMPLETE == "complete"

    def test_session_activity_values(self) -> None:
        assert SessionActivity.BUSY == "busy"
        assert SessionActivity.IDLE == "idle"
        assert SessionActivity.ERROR == "error"
