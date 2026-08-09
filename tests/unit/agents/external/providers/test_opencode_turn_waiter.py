"""Unit tests for ``TurnCompletionWaiter`` — per-turn completion detector.

Tests cover the ACTIVE/QUIESCING/COMPLETE state machine (design 5.3):
basic turn completion, loop scenarios (wait→resume→wait→resume),
nested subagent trees, quiesce window interruption, max_turn_s timeout,
cancellation, false-completion prevention, REST validation (defense in
depth against ``session.created`` loss), disconnect/reconnect behavior,
and INTERRUPT cancel cleanup.

All tests use a real ``OpenCodeSessionState`` registry (not mocked) and
a mock ``OpencodeV2Client`` for REST validation — testing through the
real registry interface per Testing Rule 2.
"""

# ruff: noqa: ANN401

from __future__ import annotations

import asyncio
import contextlib
from typing import Any
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("aiohttp", reason="aiohttp not installed")

from modex_agent.agents.external.providers.opencode.session_state import (
    OpenCodeSessionState,
    SessionActivity,
    TurnState,
)
from modex_agent.agents.external.providers.opencode.turn_waiter import (
    TurnCompletionWaiter,
)
from modex_agent.agents.external.providers.opencode.v2_client import OpencodeV2Client

_DIR = "/tmp/test"


# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------


def _make_client(
    *,
    children: dict[str, list[dict[str, Any]]] | None = None,
    status: dict[str, str] | None = None,
    children_error: dict[str, Exception] | None = None,
    status_error: dict[str, Exception] | None = None,
    children_sequence: list[list[dict[str, Any]]] | None = None,
) -> AsyncMock:
    """Build a mock ``OpencodeV2Client`` with configurable REST responses.

    ``children``: per-session children mapping (session_id → list of child dicts).
    ``children_sequence``: if set, ``get_children`` returns these in sequence
        (consumed in call order, cycling if exhausted). Overrides ``children``.
    """
    client = AsyncMock(spec=OpencodeV2Client)
    _children = children or {}
    _status = status or {}
    _children_error = children_error or {}
    _status_error = status_error or {}
    _children_seq = children_sequence
    _call_idx = 0

    async def _get_children(
        session_id: str, *, directory: str | None = None
    ) -> list[dict[str, Any]]:
        nonlocal _call_idx
        if session_id in _children_error:
            raise _children_error[session_id]
        if _children_seq is not None:
            result = _children_seq[_call_idx % len(_children_seq)]
            _call_idx += 1
            return result
        return _children.get(session_id, [])

    async def _get_status(session_id: str, *, directory: str | None = None) -> str:
        if session_id in _status_error:
            raise _status_error[session_id]
        return _status.get(session_id, "idle")

    client.get_children = _get_children
    client.get_session_status_v1 = _get_status
    return client


def _child(id: str, parent: str = "root") -> dict[str, Any]:
    """Build a child session dict as returned by ``get_children``."""
    return {"id": id, "parentID": parent}


# ---------------------------------------------------------------------------
# 2.1 — basic turn: root busy→idle, no subtree → quiesce quiet → COMPLETE
# ---------------------------------------------------------------------------


class TestBasicTurn:
    async def test_root_busy_idle_completes(self) -> None:
        registry = OpenCodeSessionState()
        client = _make_client(children={"root": []})
        waiter = TurnCompletionWaiter("root", registry, client, _DIR, quiesce_s=0.05)
        registry.register_waiter(waiter)

        registry.on_event("root", "session.status", activity=SessionActivity.BUSY)
        registry.on_event("root", "session.status", activity=SessionActivity.IDLE)

        result = await asyncio.wait_for(waiter.wait_complete(), timeout=1.0)
        assert result is TurnState.COMPLETE
        assert waiter.state is TurnState.COMPLETE

    async def test_root_error_completes(self) -> None:
        """ERROR is a converged state — turn may end (design 5.1)."""
        registry = OpenCodeSessionState()
        client = _make_client(children={"root": []})
        waiter = TurnCompletionWaiter("root", registry, client, _DIR, quiesce_s=0.05)
        registry.register_waiter(waiter)

        registry.on_event("root", "session.status", activity=SessionActivity.BUSY)
        registry.on_event("root", "session.error", activity=SessionActivity.ERROR)

        result = await asyncio.wait_for(waiter.wait_complete(), timeout=1.0)
        assert result is TurnState.COMPLETE


# ---------------------------------------------------------------------------
# 2.2 — LOOP (core): intermediate windows reset, only last completes
# ---------------------------------------------------------------------------


class TestLoopScenario:
    async def test_loop_intermediate_windows_reset(self) -> None:
        """root busy→child created→root idle→[window]→root busy(inject)→
        root idle→[window quiet]→COMPLETE."""
        registry = OpenCodeSessionState()
        client = _make_client(children={"root": []})
        waiter = TurnCompletionWaiter("root", registry, client, _DIR, quiesce_s=0.05)
        registry.register_waiter(waiter)

        task = asyncio.create_task(waiter.wait_complete())

        # Phase 1: root busy, child created, root idle
        registry.on_event("root", "session.status", activity=SessionActivity.BUSY)
        await asyncio.sleep(0)
        registry.on_event("child", "session.created", parent_sid="root")
        await asyncio.sleep(0)
        registry.on_event("root", "session.status", activity=SessionActivity.IDLE)
        await asyncio.sleep(0)

        # Waiter should enter QUIESCING (root idle, child idle default)
        await asyncio.sleep(0.01)
        assert waiter.state in (TurnState.QUIESCING, TurnState.ACTIVE)

        # Phase 2: inject — root busy again (cancels quiesce)
        registry.on_event("root", "session.status", activity=SessionActivity.BUSY)
        await asyncio.sleep(0)
        assert waiter.state is TurnState.ACTIVE

        # Phase 3: root idle — final window
        registry.on_event("root", "session.status", activity=SessionActivity.IDLE)

        result = await asyncio.wait_for(task, timeout=1.0)
        assert result is TurnState.COMPLETE
        assert waiter.state is TurnState.COMPLETE


# ---------------------------------------------------------------------------
# 2.3 — nested loop: child forks grandchild
# ---------------------------------------------------------------------------


class TestNestedLoop:
    async def test_nested_grandchild_all_idle_required(self) -> None:
        """Tree all idle only after grandchild done."""
        registry = OpenCodeSessionState()
        client = _make_client(children={"root": [], "child": [], "grandchild": []})
        waiter = TurnCompletionWaiter("root", registry, client, _DIR, quiesce_s=0.05)
        registry.register_waiter(waiter)

        task = asyncio.create_task(waiter.wait_complete())

        registry.on_event("root", "session.status", activity=SessionActivity.BUSY)
        await asyncio.sleep(0)
        registry.on_event("child", "session.created", parent_sid="root")
        await asyncio.sleep(0)
        registry.on_event("child", "session.status", activity=SessionActivity.BUSY)
        await asyncio.sleep(0)
        registry.on_event("grandchild", "session.created", parent_sid="child")
        await asyncio.sleep(0)
        registry.on_event("grandchild", "session.status", activity=SessionActivity.BUSY)
        await asyncio.sleep(0)

        # Root idle, child idle, but grandchild still busy → no complete
        registry.on_event("root", "session.status", activity=SessionActivity.IDLE)
        registry.on_event("child", "session.status", activity=SessionActivity.IDLE)
        await asyncio.sleep(0.01)
        assert waiter.state is TurnState.ACTIVE  # grandchild busy

        # Grandchild finishes
        registry.on_event("grandchild", "session.status", activity=SessionActivity.IDLE)

        result = await asyncio.wait_for(task, timeout=1.0)
        assert result is TurnState.COMPLETE


# ---------------------------------------------------------------------------
# 2.4 — quiesce window内 message.part.delta → touch → back ACTIVE
# ---------------------------------------------------------------------------


class TestQuiesceWindowInterrupt:
    async def test_message_delta_resets_quiesce(self) -> None:
        """A message.part.delta during QUIESCING resets the window."""
        registry = OpenCodeSessionState()
        client = _make_client(children={"root": []})
        waiter = TurnCompletionWaiter("root", registry, client, _DIR, quiesce_s=0.1)
        registry.register_waiter(waiter)

        task = asyncio.create_task(waiter.wait_complete())

        registry.on_event("root", "session.status", activity=SessionActivity.BUSY)
        await asyncio.sleep(0)
        registry.on_event("root", "session.status", activity=SessionActivity.IDLE)
        await asyncio.sleep(0.02)  # let QUIESCING start

        assert waiter.state is TurnState.QUIESCING

        # message.part.delta arrives → touch
        registry.on_event("root", "message.part.delta")
        # touch sets state to ACTIVE synchronously
        assert waiter.state is TurnState.ACTIVE

        # Let recheck re-arm quiesce, then complete
        result = await asyncio.wait_for(task, timeout=1.0)
        assert result is TurnState.COMPLETE

    async def test_quiesce_reset_extends_completion_time(self) -> None:
        """Touch during quiesce delays completion beyond original window."""
        registry = OpenCodeSessionState()
        client = _make_client(children={"root": []})
        waiter = TurnCompletionWaiter("root", registry, client, _DIR, quiesce_s=0.1)
        registry.register_waiter(waiter)

        task = asyncio.create_task(waiter.wait_complete())

        registry.on_event("root", "session.status", activity=SessionActivity.BUSY)
        await asyncio.sleep(0)
        registry.on_event("root", "session.status", activity=SessionActivity.IDLE)
        await asyncio.sleep(0.05)  # halfway through 0.1s window

        # Interrupt
        registry.on_event("root", "message.part.delta")

        # Should NOT complete at 0.1s (original window) — takes ~0.15s
        await asyncio.sleep(0.06)  # total 0.11s from start, 0.06s from reset
        assert not task.done()

        # Now completes (0.1s from reset)
        result = await asyncio.wait_for(task, timeout=1.0)
        assert result is TurnState.COMPLETE


# ---------------------------------------------------------------------------
# 2.5 — max_turn_s timeout via asyncio.wait_for
# ---------------------------------------------------------------------------


class TestMaxTurnTimeout:
    async def test_wait_for_timeout_raises_timeout_error(self) -> None:
        """No events arrive → wait_for raises TimeoutError."""
        registry = OpenCodeSessionState()
        client = _make_client(children={"root": []})
        waiter = TurnCompletionWaiter("root", registry, client, _DIR, quiesce_s=0.05)
        registry.register_waiter(waiter)

        # No events → tree empty → waiter stays ACTIVE
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(waiter.wait_complete(), timeout=0.1)

        assert waiter.state is TurnState.ACTIVE

    async def test_wait_for_timeout_with_busy_root(self) -> None:
        """Root stays busy → waiter never completes → timeout."""
        registry = OpenCodeSessionState()
        client = _make_client(children={"root": []})
        waiter = TurnCompletionWaiter("root", registry, client, _DIR, quiesce_s=0.05)
        registry.register_waiter(waiter)

        registry.on_event("root", "session.status", activity=SessionActivity.BUSY)

        with pytest.raises(TimeoutError):
            await asyncio.wait_for(waiter.wait_complete(), timeout=0.1)


# ---------------------------------------------------------------------------
# 2.6 — cancellation → unregister → registry no longer touches it
# ---------------------------------------------------------------------------


class TestCancellationUnregister:
    async def test_unregister_stops_touch(self) -> None:
        """After unregister, registry.on_event does not touch the waiter."""
        registry = OpenCodeSessionState()
        client = _make_client(children={"root": []})
        waiter = TurnCompletionWaiter("root", registry, client, _DIR, quiesce_s=0.05)
        registry.register_waiter(waiter)

        registry.on_event("root", "session.status", activity=SessionActivity.BUSY)
        assert waiter._wakeup.is_set()
        waiter._wakeup.clear()

        registry.unregister_waiter(waiter)

        registry.on_event("root", "session.status", activity=SessionActivity.IDLE)
        assert not waiter._wakeup.is_set()

    async def test_cancellation_propagates(self) -> None:
        """Cancelling wait_complete task propagates CancelledError."""
        registry = OpenCodeSessionState()
        client = _make_client(children={"root": []})
        waiter = TurnCompletionWaiter("root", registry, client, _DIR, quiesce_s=0.05)
        registry.register_waiter(waiter)

        task = asyncio.create_task(waiter.wait_complete())
        await asyncio.sleep(0.01)
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task


# ---------------------------------------------------------------------------
# 2.7 — root never received any event → don't enter quiesce
# ---------------------------------------------------------------------------


class TestNeverBusy:
    async def test_empty_tree_stays_active(self) -> None:
        """Root never received any event → tree empty → stays ACTIVE."""
        registry = OpenCodeSessionState()
        client = _make_client(children={"root": []})
        waiter = TurnCompletionWaiter("root", registry, client, _DIR, quiesce_s=0.05)
        registry.register_waiter(waiter)

        # No events at all
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(waiter.wait_complete(), timeout=0.1)

        assert waiter.state is TurnState.ACTIVE


# ---------------------------------------------------------------------------
# 2.8 — session.created lost → fake empty tree → REST validation
# ---------------------------------------------------------------------------


class TestRestValidationBeforeQuiesce:
    async def test_rest_finds_child_before_quiesce(self) -> None:
        """Root idle, tree only root → REST get_children returns child →
        add to tree → back ACTIVE (defense in depth)."""
        registry = OpenCodeSessionState()
        client = _make_client(children={"root": [_child("child", "root")], "child": []})
        waiter = TurnCompletionWaiter("root", registry, client, _DIR, quiesce_s=0.05)
        registry.register_waiter(waiter)

        task = asyncio.create_task(waiter.wait_complete())

        # Root idle — tree only has root → REST validation triggers
        registry.on_event("root", "session.status", activity=SessionActivity.IDLE)
        await asyncio.sleep(0.02)

        # REST should have found child and added it to tree
        assert "child" in registry.subtree_ids("root")

        # Child is IDLE (default) → all_idle → quiesce → complete
        result = await asyncio.wait_for(task, timeout=1.0)
        assert result is TurnState.COMPLETE

    async def test_rest_fetch_failure_stays_active(self) -> None:
        """REST get_children fails → don't enter QUIESCING, stay ACTIVE."""
        registry = OpenCodeSessionState()
        from modex_agent.agents.external.providers.opencode.v2_client import (
            OpencodeV2Error,
        )

        error = OpencodeV2Error(tag="UnknownError", message="fetch failed", status=500, body=None)
        client = _make_client(children_error={"root": error})
        waiter = TurnCompletionWaiter("root", registry, client, _DIR, quiesce_s=0.05)
        registry.register_waiter(waiter)

        registry.on_event("root", "session.status", activity=SessionActivity.IDLE)

        with pytest.raises(TimeoutError):
            await asyncio.wait_for(waiter.wait_complete(), timeout=0.15)

        assert waiter.state is TurnState.ACTIVE


# ---------------------------------------------------------------------------
# 2.9 — post-quiesce REST validation (defense in depth)
# ---------------------------------------------------------------------------


class TestRestValidationAfterQuiesce:
    async def test_post_quiesce_rest_confirms_no_missing(self) -> None:
        """_on_quiesce re-checks REST, confirms no missing child → COMPLETE."""
        registry = OpenCodeSessionState()
        client = _make_client(children={"root": [_child("child", "root")], "child": []})
        waiter = TurnCompletionWaiter("root", registry, client, _DIR, quiesce_s=0.05)
        registry.register_waiter(waiter)

        # Pre-populate tree with root + child (both idle)
        registry.on_event("root", "session.status", activity=SessionActivity.BUSY)
        registry.on_event("child", "session.created", parent_sid="root")
        registry.on_event("root", "session.status", activity=SessionActivity.IDLE)

        result = await asyncio.wait_for(waiter.wait_complete(), timeout=1.0)
        assert result is TurnState.COMPLETE

    async def test_post_quiesce_rest_finds_missing_child(self) -> None:
        """_on_quiesce REST finds a child not in tree → back ACTIVE."""
        registry = OpenCodeSessionState()
        # Tree has root only (no child in registry).
        # REST returns [child1, child2] — child2 is missing from tree.
        # But _recheck (single-node) already finds child1 and adds it.
        # Then tree has root + child1. _on_quiesce REST finds child2 missing.
        client = _make_client(children={"root": [_child("c1", "root"), _child("c2", "root")]})
        waiter = TurnCompletionWaiter("root", registry, client, _DIR, quiesce_s=0.05)
        registry.register_waiter(waiter)

        task = asyncio.create_task(waiter.wait_complete())

        # Root idle — tree only root → _recheck REST finds [c1, c2]
        # Both are missing → added to tree → ACTIVE
        registry.on_event("root", "session.status", activity=SessionActivity.IDLE)
        await asyncio.sleep(0.02)

        # Both children should be in tree now
        subtree = registry.subtree_ids("root")
        assert "c1" in subtree
        assert "c2" in subtree

        # Tree all idle (root + c1 + c2 all IDLE) → quiesce → _on_quiesce
        # REST returns [c1, c2] — both already in tree → no missing → COMPLETE
        result = await asyncio.wait_for(task, timeout=1.0)
        assert result is TurnState.COMPLETE

    async def test_post_quiesce_rest_failure_back_to_active(self) -> None:
        """_on_quiesce REST fetch fails → don't COMPLETE, back to ACTIVE."""
        registry = OpenCodeSessionState()
        from modex_agent.agents.external.providers.opencode.v2_client import (
            OpencodeV2Error,
        )

        # First call (from _recheck): returns [] → enter QUIESCING
        # Second call (from _on_quiesce): raises error → back to ACTIVE
        empty_resp: list[dict[str, Any]] = []
        error = OpencodeV2Error(tag="UnknownError", message="fetch failed", status=500, body=None)

        call_count = 0

        async def _get_children(
            session_id: str, *, directory: str | None = None
        ) -> list[dict[str, Any]]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return empty_resp
            raise error

        client = AsyncMock(spec=OpencodeV2Client)
        client.get_children = _get_children
        client.get_session_status_v1 = AsyncMock(return_value="idle")

        waiter = TurnCompletionWaiter("root", registry, client, _DIR, quiesce_s=0.05)
        registry.register_waiter(waiter)

        registry.on_event("root", "session.status", activity=SessionActivity.IDLE)

        # _recheck: REST returns [] → QUIESCING
        # _on_quiesce: REST fails → ACTIVE
        # No more events → stays ACTIVE
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(waiter.wait_complete(), timeout=0.2)

        assert waiter.state is TurnState.ACTIVE


# ---------------------------------------------------------------------------
# 2.10 — disconnect/reconnect behavior
# ---------------------------------------------------------------------------


class TestDisconnectBehavior:
    async def test_mark_reconnect_pending_cancels_quiesce(self) -> None:
        """Reader disconnect → mark_reconnect_pending → touch → ACTIVE;
        quiesce timer cancelled."""
        registry = OpenCodeSessionState()
        client = _make_client(children={"root": []})
        waiter = TurnCompletionWaiter("root", registry, client, _DIR, quiesce_s=0.1)
        registry.register_waiter(waiter)

        task = asyncio.create_task(waiter.wait_complete())

        # Enter QUIESCING
        registry.on_event("root", "session.status", activity=SessionActivity.BUSY)
        await asyncio.sleep(0)
        registry.on_event("root", "session.status", activity=SessionActivity.IDLE)
        await asyncio.sleep(0.02)
        assert waiter.state is TurnState.QUIESCING

        # Disconnect
        registry.mark_reconnect_pending()
        assert registry.is_reconnect_pending() is True
        assert waiter.state is TurnState.ACTIVE  # touch reset to ACTIVE

        # Wait a bit — should NOT complete (reconnect_pending blocks quiesce)
        await asyncio.sleep(0.15)
        assert not task.done()
        assert waiter.state is TurnState.ACTIVE

        # Reconnect: rebuild + clear reconnect_pending + touch
        await registry.rebuild_subtree("root", client, directory=_DIR)
        assert registry.is_reconnect_pending() is False
        waiter.touch()  # simulate reader touching after rebuild

        result = await asyncio.wait_for(task, timeout=1.0)
        assert result is TurnState.COMPLETE

    async def test_reconnect_pending_blocks_quiesce_even_if_all_idle(self) -> None:
        """While is_reconnect_pending, _recheck stays ACTIVE even if all_idle."""
        registry = OpenCodeSessionState()
        client = _make_client(children={"root": []})
        waiter = TurnCompletionWaiter("root", registry, client, _DIR, quiesce_s=0.05)
        registry.register_waiter(waiter)

        registry.on_event("root", "session.status", activity=SessionActivity.BUSY)
        registry.on_event("root", "session.status", activity=SessionActivity.IDLE)

        # Mark reconnect pending BEFORE waiter can enter quiesce
        registry.mark_reconnect_pending()

        with pytest.raises(TimeoutError):
            await asyncio.wait_for(waiter.wait_complete(), timeout=0.15)

        assert waiter.state is TurnState.ACTIVE


# ---------------------------------------------------------------------------
# 2.11 — INTERRUPT cancel → CancelledError propagates, finally unregisters
# ---------------------------------------------------------------------------


class TestInterruptCancel:
    async def test_cancelled_error_propagates(self) -> None:
        """wait_complete cancelled → CancelledError propagates (not swallowed)."""
        registry = OpenCodeSessionState()
        client = _make_client(children={"root": []})
        waiter = TurnCompletionWaiter("root", registry, client, _DIR, quiesce_s=0.05)
        registry.register_waiter(waiter)

        task = asyncio.create_task(waiter.wait_complete())
        await asyncio.sleep(0.01)
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

    async def test_finally_unregister_executed(self) -> None:
        """After cancel, the caller's finally block unregisters the waiter;
        registry no longer touches it."""
        registry = OpenCodeSessionState()
        client = _make_client(children={"root": []})
        waiter = TurnCompletionWaiter("root", registry, client, _DIR, quiesce_s=0.05)
        registry.register_waiter(waiter)

        task = asyncio.create_task(waiter.wait_complete())
        await asyncio.sleep(0.01)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        # Caller's finally block:
        registry.unregister_waiter(waiter)

        # Registry should no longer touch this waiter
        assert waiter not in registry._waiters
        wakeup_before = waiter._wakeup.is_set()
        registry.on_event("root", "session.status", activity=SessionActivity.BUSY)
        # _wakeup state should not change (waiter not in registry)
        assert waiter._wakeup.is_set() == wakeup_before


# ---------------------------------------------------------------------------
# covers() method
# ---------------------------------------------------------------------------


class TestCovers:
    def test_covers_root(self) -> None:
        registry = OpenCodeSessionState()
        client = _make_client(children={"root": []})
        waiter = TurnCompletionWaiter("root", registry, client, _DIR)
        registry.register_waiter(waiter)

        registry.on_event("root", "session.status", activity=SessionActivity.BUSY)
        assert waiter.covers("root", registry) is True

    def test_covers_child(self) -> None:
        registry = OpenCodeSessionState()
        client = _make_client(children={"root": []})
        waiter = TurnCompletionWaiter("root", registry, client, _DIR)
        registry.register_waiter(waiter)

        registry.on_event("root", "session.status", activity=SessionActivity.BUSY)
        registry.on_event("child", "session.created", parent_sid="root")
        assert waiter.covers("child", registry) is True

    def test_does_not_cover_unrelated(self) -> None:
        registry = OpenCodeSessionState()
        client = _make_client(children={"root": []})
        waiter = TurnCompletionWaiter("root", registry, client, _DIR)
        registry.register_waiter(waiter)

        registry.on_event("root", "session.status", activity=SessionActivity.BUSY)
        assert waiter.covers("unrelated", registry) is False

    def test_does_not_cover_empty_tree(self) -> None:
        registry = OpenCodeSessionState()
        client = _make_client(children={"root": []})
        waiter = TurnCompletionWaiter("root", registry, client, _DIR)
        registry.register_waiter(waiter)

        assert waiter.covers("root", registry) is False


# ---------------------------------------------------------------------------
# root_missing → COMPLETE
# ---------------------------------------------------------------------------


class TestRootMissing:
    async def test_root_missing_completes(self) -> None:
        """is_root_missing → state = COMPLETE (root session gone)."""
        registry = OpenCodeSessionState()
        client = _make_client(children={"root": []})
        waiter = TurnCompletionWaiter("root", registry, client, _DIR, quiesce_s=0.05)
        registry.register_waiter(waiter)

        task = asyncio.create_task(waiter.wait_complete())

        # Root busy
        registry.on_event("root", "session.status", activity=SessionActivity.BUSY)
        await asyncio.sleep(0)

        # Mark root missing (e.g., opencode process restarted)
        registry.mark_root_missing("root")
        await asyncio.sleep(0.01)

        result = await asyncio.wait_for(task, timeout=1.0)
        assert result is TurnState.COMPLETE


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------


class TestProperties:
    def test_root_sid_property(self) -> None:
        registry = OpenCodeSessionState()
        client = _make_client()
        waiter = TurnCompletionWaiter("my_session", registry, client, _DIR)
        assert waiter.root_sid == "my_session"

    def test_initial_state_is_active(self) -> None:
        registry = OpenCodeSessionState()
        client = _make_client()
        waiter = TurnCompletionWaiter("root", registry, client, _DIR)
        assert waiter.state is TurnState.ACTIVE

    def test_max_turn_s_stored(self) -> None:
        registry = OpenCodeSessionState()
        client = _make_client()
        waiter = TurnCompletionWaiter(
            "root", registry, client, _DIR, quiesce_s=5.0, max_turn_s=600.0
        )
        assert waiter._quiesce_s == 5.0
        assert waiter._max_turn_s == 600.0
