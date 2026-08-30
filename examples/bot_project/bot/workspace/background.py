"""Re-homed BackgroundTaskRunner (REHOME; purely additive).

Re-homes — FAITHFULLY, logic verbatim — the dream background-task logic
that lived on the old ``Workspace`` class
(:mod:`bot.workspace.pool_data`). Standalone, testable unit: the
runner owns its own ``dream_engine`` / interval / stop_event, driven by
already-built :class:`PoolData` (built by
:func:`bot.workspace.pool_data.build_pool_data`).

The curator half died with the experience capability's supply face
(SPEC §8.3 D4): ``ExperienceSupply`` owns the per-pool curator loop now
— ``ExperienceCapability.supply()`` constructs it, pool assembly starts
it, pool teardown (``AgentPool.shutdown_all``) stops it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from bot.workspace.pool_data import PoolData
from modex_agent.memory.consolidation.dream_engine import DreamEngine
from modex_agent.multi_agent.pool_config.deps import PoolAssemblyDeps

logger = logging.getLogger(__name__)

# Fallback interval (seconds) when pool config does not specify one.
# Re-homed verbatim from the old Workspace module.
_DEFAULT_DREAM_INTERVAL = 1800


class BackgroundTaskRunner:
    """Run the workspace dream background loop.

    Re-homed FAITHFULLY from ``Workspace`` (``_maybe_build_dream`` /
    ``start_background_tasks`` / ``stop_background_tasks`` /
    ``_dream_background_loop``). Construction builds the dream engine from the
    first pool with archive + core memory enabled. The per-pool experience
    curator loops moved to the experience capability supply
    (``ExperienceSupply.start``/``stop`` — SPEC §8.3 D4).
    """

    def __init__(
        self,
        *,
        pool_data: dict[str, PoolData],
        assembly_deps: dict[str, PoolAssemblyDeps],
        default_pool_name: str | None,
    ) -> None:
        self._pool_data: dict[str, PoolData] = pool_data
        self._assembly_deps: dict[str, PoolAssemblyDeps] = assembly_deps
        self._default_pool_name: str | None = default_pool_name

        # Built eagerly from pool_data (re-home of _maybe_build_dream).
        # Exposed for lifecycle tests + CUTOVER wiring.
        self.dream_engine: DreamEngine | None = None
        self._dream_interval: int = _DEFAULT_DREAM_INTERVAL

        # Background-task bookkeeping.
        self._tasks: list[asyncio.Task[None]] = []
        self._stop_event: asyncio.Event = asyncio.Event()

    # ------------------------------------------------------------------
    # Build helpers (re-homed from Workspace)
    # ------------------------------------------------------------------

    def _maybe_build_dream(self) -> DreamEngine | None:
        """Build the workspace DreamEngine from the first pool with archive + core memory enabled.

        Scans all pools in ``self._assembly_deps`` to find the first one where
        both ``archive`` and ``core`` memory configs are enabled. Uses that
        pool's memory system to build the DreamEngine. Returns ``None`` when
        no pool qualifies or the pool data has not been built yet.
        """
        for name, deps in self._assembly_deps.items():
            if deps.memory is None:
                continue
            memory_cfg = deps.memory
            if memory_cfg.archive is None or not memory_cfg.archive.enabled:
                continue
            if memory_cfg.core is None or not memory_cfg.core.enabled:
                continue

            pool_data = self._pool_data.get(name)
            if pool_data is None:
                continue
            memory_system = pool_data.context_manager.memory_system
            archive_manager = memory_system.archive_manager
            core_memory_manager = memory_system.core_memory_manager
            if archive_manager is None or core_memory_manager is None:
                continue

            dream_cfg = memory_cfg.dream_engine
            self._dream_interval = (
                dream_cfg.interval if dream_cfg is not None else _DEFAULT_DREAM_INTERVAL
            )
            engine = DreamEngine(
                history_manager=archive_manager,
                long_term_manager=core_memory_manager,
                registry=memory_system.store_registry,
                max_consume_per_run=(dream_cfg.max_consume_per_run if dream_cfg is not None else 3),
                consolidator=memory_system.core_memory_consolidator,
            )
            self.dream_engine = engine
            logger.info(
                "Workspace DreamEngine initialized, pool=%s, interval=%ds",
                name,
                self._dream_interval,
            )
            return engine
        return None

    # ------------------------------------------------------------------
    # Lifecycle (re-homed from Workspace)
    # ------------------------------------------------------------------

    @property
    def tasks(self) -> list[asyncio.Task[None]]:
        """Read-only view of currently running background tasks."""
        return self._tasks

    async def start(self) -> None:
        """Build (if needed) and launch the dream background loop.

        Re-homed verbatim from ``Workspace.start_background_tasks``. Guards
        against double-start: if tasks are already running this is a no-op.
        """
        if self._tasks:
            return  # already started

        if self.dream_engine is None:
            self._maybe_build_dream()

        self._stop_event.clear()

        if self.dream_engine is not None:
            self._tasks.append(
                asyncio.create_task(
                    self._dream_loop(self._dream_interval),
                    name="workspace-dream",
                )
            )

    async def stop(self) -> None:
        """Cancel and await all background tasks, then clear bookkeeping.

        Re-homed verbatim from ``Workspace.stop_background_tasks``. Idempotent:
        safe to call when nothing is running.
        """
        self._stop_event.set()
        tasks = self._tasks
        self._tasks = []
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    # ------------------------------------------------------------------
    # Loops (re-homed from Workspace)
    # ------------------------------------------------------------------

    async def _wait_tick(self, interval: int) -> bool:
        """Sleep for *interval* seconds and return ``True`` unless stopped."""
        await asyncio.sleep(interval)
        return not self._stop_event.is_set()

    async def _dream_loop(self, interval: int) -> None:
        """Periodically run ``dream_engine.scan_all`` until stopped.

        Re-homed verbatim from ``Workspace._dream_background_loop``.
        """
        engine = self.dream_engine
        if engine is None:
            return
        while await self._wait_tick(interval):
            try:
                processed = await engine.scan_all()
                if processed:
                    logger.info(
                        "Workspace DreamEngine processed %d scope(s)",
                        len(processed),
                    )
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Workspace DreamEngine background loop error")
