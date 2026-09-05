"""Per-run cooperative control handle for graph schedulers."""

from __future__ import annotations

import asyncio
from collections.abc import Collection

from .exceptions import GraphBubbleUp, GraphDrained


class GraphRunControl:
    """Carry irreversible pause and stop signals into scheduler safe points."""

    def __init__(self) -> None:
        self._pause_requested: bool = False
        self._stop_requested: bool = False
        self._drain_reason: str | None = None
        self._wakeup = asyncio.Event()

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
        """Set the wait event before scheduling; None restores an owned event."""
        self._wakeup = wakeup if wakeup is not None else asyncio.Event()

    def check(self) -> None:
        """Raise the single cooperative drain signal at a scheduler safe point."""
        if not self._pause_requested and not self._stop_requested:
            return
        reason = self._drain_reason
        if reason is None:
            reason = "graph drain requested"
        raise GraphDrained(reason)

    async def wait_for_tasks(
        self, tasks: Collection[asyncio.Task[None]]
    ) -> set[asyncio.Task[None]]:
        """Wait for work or control/deliver activity, without owning node cancellation."""
        self.check()
        wakeup = asyncio.create_task(self._wakeup.wait())
        try:
            await asyncio.wait(
                {*tasks, wakeup},
                return_when=asyncio.FIRST_COMPLETED,
            )
            self._wakeup.clear()
        finally:
            wakeup.cancel()
            await asyncio.gather(wakeup, return_exceptions=True)
        done = {task for task in tasks if task.done()}
        # Surface node faults/interrupts before drain signals from other ready
        # tasks. Deferred drain/cancellation results remain owned by the caller.
        for task in done:
            try:
                task.result()
            except (GraphDrained, asyncio.CancelledError):
                continue
        self.check()
        return done

    async def cancel_and_drain(self, tasks: Collection[asyncio.Task[None]]) -> None:
        """Cancel once, drain every child, then propagate cleanup faults.

        The drain task is retained and shielded until it settles. Repeated owner
        cancellation cannot issue a second cancellation into child cleanup.
        """
        tasks = tuple(tasks)
        for task in tasks:
            if not task.done() and not task.cancelling():
                task.cancel()
        if not tasks:
            return

        async def drain() -> None:
            await asyncio.gather(*tasks, return_exceptions=True)
            interruption: GraphBubbleUp | None = None
            fault: Exception | None = None
            for task in tasks:
                try:
                    task.result()
                except (GraphDrained, asyncio.CancelledError):
                    pass
                except GraphBubbleUp as exc:
                    interruption = exc
                except Exception as exc:
                    fault = exc
            if fault is not None:
                raise fault
            if interruption is not None:
                raise interruption

        await self.wait_for_settlement(asyncio.create_task(drain()))

    @staticmethod
    async def wait_for_settlement[T](task: asyncio.Future[T]) -> T:
        """Finish owned cleanup despite caller cancellation; faults take precedence.

        Cancellation is delayed, not swallowed. This is the shared settlement
        boundary for scheduler drain and the orchestrator's finalization task.
        """
        cancellation: asyncio.CancelledError | None = None
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as exc:
                cancellation = exc
            except Exception:
                break
        result = task.result()
        if cancellation is not None:
            raise cancellation
        return result

    def _wake(self) -> None:
        self._wakeup.set()


__all__ = ["GraphRunControl"]
