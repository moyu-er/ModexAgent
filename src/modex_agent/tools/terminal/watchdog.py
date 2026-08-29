"""Background watchdog closing terminal tabs whose command deadline expired."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

from modex_agent.tools.terminal.managers import TerminalManagerBase
from modex_agent.tools.terminal.process_registry import ProcessRegistry
from modex_agent.tools.terminal.types import ProcessStatus

logger = logging.getLogger(__name__)


class TerminalWatchdog:
    """Run one pool-scoped scanner for command deadline expiry."""

    def __init__(
        self,
        manager: TerminalManagerBase,
        registry: ProcessRegistry,
        *,
        interval_s: float = 5.0,
    ) -> None:
        self._manager = manager
        self._registry = registry
        self._interval_s = interval_s
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Start the scanner once from within a running event loop."""
        if self._task is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            raise RuntimeError("TerminalWatchdog.start() requires a running event loop") from None
        self._task = loop.create_task(self._run())

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._interval_s)
            for session in self._registry.list_running():
                if time.monotonic() < session.deadline_at:
                    continue
                try:
                    await self._manager.close(session.terminal)
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "watchdog: failed to close expired terminal tab %s",
                        session.terminal,
                    )
                    continue
                self._registry.mark_exited(
                    session.id,
                    exit_code=None,
                    exit_signal="TIMEOUT",
                    status=ProcessStatus.TIMED_OUT,
                    timed_out=True,
                )

    async def stop(self) -> None:
        """Cancel and await the scanner if it was started."""
        task = self._task
        if task is None:
            return
        self._task = None
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
