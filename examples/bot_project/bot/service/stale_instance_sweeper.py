"""Stale active graph instance sweeper.

Periodic business-layer task that scans RUNNING/PAUSING/STOPPING instances and marks
stale ones as CRASHED. An instance is stale when its ``executor_process_id``
attr is either absent (NULL — no process ever claimed it) or belongs to a
process no longer in the alive set.

The sweeper ONLY updates status to CRASHED — it does NOT trigger recovery
(recovery is explicit via ``GraphOrchestrator.recover_crashed()``). Terminal
states (COMPLETED/FAILED/CRASHED/STOPPED) are never scanned, so their
``executor_process_id`` attrs are preserved as audit trail.

NULL executor (``attrs`` has no ``executor_process_id`` key, or the value is
``None``) is treated as dirty — the instance was never claimed by any live
process, so it is swept to CRASHED.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from modex_agent.runtime.constants import EXECUTOR_PROCESS_ID_KEY
from modex_graph import GraphInstanceStatus, GraphInstanceStore

if TYPE_CHECKING:
    from modex_agent.runtime.process_registry import ProcessRegistry

logger = logging.getLogger(__name__)


class StaleInstanceSweeper:
    """Scan active graph instances and mark stale ones CRASHED.

    Args:
        instance_store: The graph instance store to scan and update.
        process_registry: Registry reporting which process IDs are alive.
    """

    def __init__(
        self,
        instance_store: GraphInstanceStore,
        process_registry: ProcessRegistry,
    ) -> None:
        self._instance_store = instance_store
        self._process_registry = process_registry

    async def sweep(self) -> list[int]:
        """Scan RUNNING/PAUSING/STOPPING instances, mark stale ones CRASHED.

        Returns the list of swept ``graph_instance_id``s. An instance is
        stale when ``executor_process_id`` is NULL (absent/None) or not in
        the alive set. Alive instances are left untouched.
        """
        running = [
            metadata
            for status in (
                GraphInstanceStatus.RUNNING, GraphInstanceStatus.PAUSING, GraphInstanceStatus.STOPPING,
            )
            for metadata in self._instance_store.load_by_status(status)
        ]
        if not running:
            return []
        alive = self._process_registry.alive_process_ids()
        swept: list[int] = []
        for meta in running:
            executor_pid = meta.attrs.get(EXECUTOR_PROCESS_ID_KEY)
            if executor_pid is None or executor_pid not in alive:
                self._instance_store.update_status(
                    meta.graph_instance_id, GraphInstanceStatus.CRASHED
                )
                swept.append(meta.graph_instance_id)
        if swept:
            logger.info(
                "stale_instance_sweeper: marked %d active instance(s) CRASHED: %s",
                len(swept),
                swept,
            )
        return swept


def start_sweeper_loop(
    sweeper: StaleInstanceSweeper,
    interval_seconds: float,
) -> asyncio.Task[None]:
    """Launch a periodic sweeper loop as a background task.

    The loop calls ``sweeper.sweep()`` every ``interval_seconds`` until
    cancelled. Errors during a single sweep are logged and swallowed so
    the loop continues. Returns the task so the caller can cancel it on
    shutdown.
    """
    async def _loop() -> None:
        while True:
            await asyncio.sleep(interval_seconds)
            try:
                await sweeper.sweep()
            except Exception:
                logger.warning(
                    "stale_instance_sweeper: sweep raised; continuing loop",
                    exc_info=True,
                )

    return asyncio.create_task(_loop(), name="stale-instance-sweeper")


__all__ = ["StaleInstanceSweeper", "start_sweeper_loop"]
