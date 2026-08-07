"""``TurnCompletionWaiter`` — per-turn completion detector (design 5.3).

ACTIVE/QUIESCING/COMPLETE state machine that hangs on an ``asyncio.Event``
(zero CPU) and is woken by ``registry.touch()``. The waiter self-checks
its state machine on each wake — the registry never decides completion,
it only notifies.

State machine semantics (design 5.3):

    Initial ACTIVE (root_sid already register_waiter'd)
      loop:
        await _wakeup.wait()          # zero-poll sleep
        _wakeup.clear()
        if _state == COMPLETE: return COMPLETE
        await _recheck()

    _recheck (reads registry state, may do async REST):
      is_root_missing(root)   → COMPLETE (root session gone)
      is_reconnect_pending()  → stay ACTIVE (don't quiesce while disconnected)
      tree empty              → stay ACTIVE (prevents prompt→idle race)
      all_idle(root):
          not QUIESCING → enter QUIESCING (arm call_later timer)
              ★ single-node tree → REST get_children validation first
                 (defense against session.created loss → fake empty tree)
          already QUIESCING → timer is running, no-op
      not all_idle            → ACTIVE, cancel any pending quiesce timer

    _on_quiesce (timer fires):
      ★二次校验: all_idle still True? REST get_children confirms no
      missing child? → COMPLETE. Otherwise → ACTIVE (window saw events
      or REST found a missing child).

    touch (any tree event from registry.on_event):
      set _wakeup; cancel pending quiesce timer; reset QUIESCING→ACTIVE.
      The next _recheck re-evaluates from ACTIVE.

    max_turn_s: handled by the caller via ``asyncio.wait_for`` — the
    waiter itself does not implement timeout. CancelledError propagates
    naturally; ``execute_streaming``'s finally block calls
    ``unregister_waiter``.

Key design point — ★ REST validation (the ONLY fatal gap in tree-quiescence):
``session.created`` loss → registry sees empty tree → ``all_idle`` falsely
True → fake COMPLETE → subagent inject output lost. Two-layer defense:

1. Before QUIESCING: if tree has only root (no child ever seen), REST
   ``get_children(root)``. Child found → add to tree → ACTIVE. Fetch
   failure → stay ACTIVE (wait for rebuild).
2. After quiesce window: ``_on_quiesce`` re-checks ``all_idle`` AND REST
   ``get_children(root)`` confirms no missing child → only then COMPLETE.
   Fetch failure → don't COMPLETE, back to ACTIVE.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from .session_state import OpenCodeSessionState, TurnState
from .v2_client import OpencodeV2Client

logger = logging.getLogger(__name__)

__all__ = ["TurnCompletionWaiter"]


class TurnCompletionWaiter:
    """Per-turn completion detector with ACTIVE/QUIESCING/COMPLETE state machine.

    Hangs on ``asyncio.Event`` (zero CPU); woken by ``registry.touch()``.
    Quiesce window via ``asyncio.create_task(asyncio.sleep(...))``;
    any ``touch()`` cancels it and resets to ACTIVE. The waiter is
    use-and-discard — ``execute_streaming`` creates one per turn and
    unregisters it in the finally block.
    """

    def __init__(
        self,
        root_sid: str,
        registry: OpenCodeSessionState,
        client: OpencodeV2Client,
        directory: str,
        quiesce_s: float = 3.0,
        max_turn_s: float | None = 1800.0,
    ) -> None:
        self._root_sid = root_sid
        self._registry = registry
        self._client = client
        self._directory = directory
        self._quiesce_s = quiesce_s
        self._max_turn_s = max_turn_s
        self._state: TurnState = TurnState.ACTIVE
        self._wakeup: asyncio.Event = asyncio.Event()
        self._quiesce_task: asyncio.Task[None] | None = None

    @property
    def root_sid(self) -> str:
        return self._root_sid

    @property
    def state(self) -> TurnState:
        return self._state

    def covers(self, sid: str, registry: OpenCodeSessionState) -> bool:
        """True if ``sid`` is in this waiter's subtree (via registry)."""
        return sid in registry.subtree_ids(self._root_sid)

    def touch(self) -> None:
        """Called by ``registry.on_event`` for any tree event.

        Sets ``_wakeup`` so ``wait_complete`` re-evaluates, cancels any
        pending quiesce timer, and resets QUIESCING→ACTIVE so the next
        ``_recheck`` starts fresh (design 5.3: "下次 _recheck 重新判定").
        """
        self._wakeup.set()
        self._cancel_quiesce()
        if self._state is TurnState.QUIESCING:
            self._state = TurnState.ACTIVE

    async def wait_complete(self) -> TurnState:
        """Await turn completion. Returns ``TurnState.COMPLETE`` when done.

        Zero-poll: hangs on ``_wakeup.wait()``. On each wake, clears the
        event, checks for COMPLETE, and calls ``_recheck`` to transition.

        ``CancelledError`` is NOT caught — it propagates to the caller
        (``execute_streaming``'s finally block handles ``unregister_waiter``).
        The quiesce task is cleaned up in the finally block here.
        """
        try:
            while True:
                await self._wakeup.wait()
                self._wakeup.clear()
                if self._state is TurnState.COMPLETE:
                    return TurnState.COMPLETE
                await self._recheck()
        finally:
            self._cancel_quiesce()

    # ------------------------------------------------------------------
    # State machine — _recheck (reads registry, may do async REST)
    # ------------------------------------------------------------------

    async def _recheck(self) -> None:
        """Re-evaluate state from the registry's current view.

        Pure from the registry's perspective (reads only, no mutations
        to existing nodes). May do async REST ``get_children`` for
        single-node trees (defense against ``session.created`` loss).
        Adding newly discovered children goes through ``registry.on_event``
        which is the registry's own mutation path.
        """
        # Root session gone → can't continue → COMPLETE
        if self._registry.is_root_missing(self._root_sid):
            self._state = TurnState.COMPLETE
            self._wakeup.set()
            return

        # Reader disconnected → don't enter QUIESCING (wait for reconnect)
        if self._registry.is_reconnect_pending():
            self._cancel_quiesce()
            return

        subtree = self._registry.subtree_ids(self._root_sid)
        if not subtree:
            # Tree empty (root not seen yet) → stay ACTIVE
            # Prevents "prompt → immediate idle" false completion race
            self._cancel_quiesce()
            return

        if self._registry.all_idle(self._root_sid):
            # All idle → enter QUIESCING (if not already)
            if self._state is not TurnState.QUIESCING:
                # ★ REST validation for single-node trees
                # (defense against session.created loss → fake empty tree)
                if len(subtree) == 1:
                    children = await self._fetch_children()
                    if children is None:
                        # Fetch failed → stay ACTIVE, wait for rebuild
                        return
                    new_children = self._filter_missing_children(children, subtree)
                    if new_children:
                        self._add_children_from_rest(new_children)
                        # Children added → back to ACTIVE.
                        # on_event touched us (_wakeup set) → next _recheck
                        # will re-evaluate with the expanded tree.
                        return
                # No missing children (or multi-node tree) → enter QUIESCING
                self._state = TurnState.QUIESCING
                self._arm_quiesce()
            # Already QUIESCING → timer is running, no-op
        else:
            # Not all idle (some node BUSY) → ACTIVE, cancel quiesce
            self._state = TurnState.ACTIVE
            self._cancel_quiesce()

    # ------------------------------------------------------------------
    # State machine — _on_quiesce (timer fires, final validation)
    # ------------------------------------------------------------------

    async def _on_quiesce_async(self) -> None:
        """Quiesce window elapsed — final defense-in-depth validation.

        Re-checks ``all_idle`` AND REST ``get_children`` confirms no
        missing child. Only then COMPLETE. Any discrepancy → ACTIVE
        (window saw events, or REST discovered a missing child).
        """
        # State might have been changed by touch during disconnect
        if self._state is not TurnState.QUIESCING:
            return

        # Root missing → COMPLETE
        if self._registry.is_root_missing(self._root_sid):
            self._state = TurnState.COMPLETE
            self._wakeup.set()
            return

        # all_idle still True? (event during window would make it False)
        if not self._registry.all_idle(self._root_sid):
            self._state = TurnState.ACTIVE
            self._wakeup.set()
            return

        # ★ REST confirm no missing child
        children = await self._fetch_children()

        # State might have changed during await (touch from reconnect)
        if self._state is not TurnState.QUIESCING:
            return

        if children is None:
            # Fetch failed → don't COMPLETE, back to ACTIVE
            self._state = TurnState.ACTIVE
            self._wakeup.set()
            return

        known = self._registry.subtree_ids(self._root_sid)
        missing = self._filter_missing_children(children, known)
        if missing:
            self._add_children_from_rest(missing)
            return

        # All confirmed → COMPLETE
        self._state = TurnState.COMPLETE
        self._wakeup.set()

    # ------------------------------------------------------------------
    # REST helpers
    # ------------------------------------------------------------------

    async def _fetch_children(self) -> list[dict[str, Any]] | None:
        """REST ``get_children(root_sid)``. Returns None on fetch failure.

        ``null ≠ {}`` — a failed fetch never becomes a fake empty list.
        """
        try:
            return await self._client.get_children(self._root_sid, directory=self._directory)
        except Exception:  # noqa: BLE001 -- REST may fail transiently
            logger.exception("TurnCompletionWaiter: get_children failed for %s", self._root_sid)
            return None

    @staticmethod
    def _filter_missing_children(
        children: list[dict[str, Any]], known: frozenset[str]
    ) -> list[dict[str, Any]]:
        """Extract child dicts whose ``id`` is not in the known subtree."""
        return [
            c
            for c in children
            if isinstance(c, dict)
            and isinstance(c.get("id"), str)
            and c["id"]
            and c["id"] not in known
        ]

    def _add_children_from_rest(self, children: list[dict[str, Any]]) -> None:
        """Add newly discovered children to the registry tree.

        Uses ``registry.on_event`` with ``session.created`` — the
        registry's own mutation path. Children are added as IDLE
        (default); future ``session.status`` SSE events will update
        their activity. The 3s quiesce window is long enough for a
        BUSY status event to arrive (same-process inject is ms-level).
        """
        for child in children:
            child_id = child.get("id")
            if not isinstance(child_id, str) or not child_id:
                continue
            self._registry.on_event(child_id, "session.created", parent_sid=self._root_sid)

    # ------------------------------------------------------------------
    # Quiesce timer management
    # ------------------------------------------------------------------

    def _arm_quiesce(self) -> None:
        """Arm the quiesce timer: after ``quiesce_s``, run ``_on_quiesce_async``."""
        self._cancel_quiesce()
        self._quiesce_task = asyncio.create_task(self._quiesce_timer())

    def _cancel_quiesce(self) -> None:
        """Cancel any pending quiesce timer."""
        if self._quiesce_task is not None and not self._quiesce_task.done():
            self._quiesce_task.cancel()
        self._quiesce_task = None

    async def _quiesce_timer(self) -> None:
        """Sleep for ``quiesce_s`` then run ``_on_quiesce_async``.

        Cancelled by ``_cancel_quiesce`` (from ``touch`` or state transitions).
        Handles ``CancelledError`` gracefully — a cancelled timer is normal.
        """
        try:
            await asyncio.sleep(self._quiesce_s)
        except asyncio.CancelledError:
            return
        try:
            await self._on_quiesce_async()
        except asyncio.CancelledError:
            return  # timer cancelled while doing REST validation
        except Exception:  # noqa: BLE001 -- don't let _on_quiesce kill silently
            logger.exception(
                "TurnCompletionWaiter: _on_quiesce_async failed for %s",
                self._root_sid,
            )
            # Don't get stuck — go back to ACTIVE and wake up for retry
            if self._state is TurnState.QUIESCING:
                self._state = TurnState.ACTIVE
                self._wakeup.set()
