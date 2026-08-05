"""Per-run cooperative control handle for graph schedulers."""

from __future__ import annotations

import asyncio

from .exceptions import GraphDrained


class GraphRunControl:
    """Carry irreversible pause and stop signals into scheduler safe points."""

    def __init__(self) -> None:
        self._pause_requested: bool = False
        self._stop_requested: bool = False
        self._drain_reason: str | None = None
        self._wakeup: asyncio.Event | None = None

    @property
    def pause_requested(self) -> bool:
        """Whether pause has been requested for this run."""
        return self._pause_requested

    @property
    def stop_requested(self) -> bool:
        """Whether stop has been requested for this run."""
        return self._stop_requested

    @property
    def drain_reason(self) -> str | None:
        """The latest reason supplied with a drain request."""
        return self._drain_reason

    def request_pause(self, reason: str) -> None:
        """Request pause and wake a blocked scheduler."""
        self._pause_requested = True
        self._drain_reason = reason
        self._wake()

    def request_stop(self, reason: str) -> None:
        """Request stop and wake a blocked scheduler."""
        self._stop_requested = True
        self._drain_reason = reason
        self._wake()

    def notify_deliver(self, target: str) -> None:
        """Wake the scheduler after an external deliver is persisted."""
        self._wake()

    def set_wakeup(self, wakeup: asyncio.Event | None) -> None:
        """Attach the scheduler event used by external control signals."""
        self._wakeup = wakeup

    def check(self) -> None:
        """Raise the single cooperative drain signal at a scheduler safe point."""
        if not self._pause_requested and not self._stop_requested:
            return
        reason = self._drain_reason
        if reason is None:
            reason = "graph drain requested"
        raise GraphDrained(reason)

    def _wake(self) -> None:
        if self._wakeup is not None:
            self._wakeup.set()


__all__ = ["GraphRunControl"]
