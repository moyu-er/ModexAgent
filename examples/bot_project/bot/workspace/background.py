"""Re-homed BackgroundTaskRunner (REHOME; purely additive).

Re-homes — FAITHFULLY, logic verbatim — the dream/curator background-task logic
that lived on the old ``Workspace`` class
(:mod:`bot.workspace.pool_data`). Standalone, testable unit: the
runner owns its own ``dream_engine`` / ``curators`` / intervals / stop_event,
driven by already-built :class:`PoolData` (built by
:func:`bot.workspace.pool_data.build_pool_data`).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from bot.workspace.pool_data import PoolData
from modex_agent.core.experience import ExperienceCurator
from modex_agent.ioc.configs.pool import PoolConfig
from modex_agent.memory.consolidation.dream_engine import DreamEngine

logger = logging.getLogger(__name__)

# Fallback intervals (seconds) when pool config does not specify one.
# Re-homed verbatim from the old Workspace module.
_DEFAULT_DREAM_INTERVAL = 1800
_DEFAULT_CURATOR_INTERVAL = 3600


class BackgroundTaskRunner:
    """Run the workspace dream + per-pool curator background loops.

    Re-homed FAITHFULLY from ``Workspace`` (``_maybe_build_dream`` /
    ``_build_curators`` / ``start_background_tasks`` /
    ``stop_background_tasks`` / ``_dream_background_loop`` /
    ``_curator_background_loop``). Construction builds the dream engine from the
    default pool's ``pool_data.context_manager.memory_system`` and one curator
    per pool whose main agent enables experience.
    """

    def __init__(
        self,
        *,
        pool_data: dict[str, PoolData],
        pools_config: dict[str, PoolConfig],
        default_pool_name: str | None,
    ) -> None:
        self._pool_data: dict[str, PoolData] = pool_data
        self._pools_config: dict[str, PoolConfig] = pools_config
        self._default_pool_name: str | None = default_pool_name

        # Built eagerly from pool_data (re-home of _maybe_build_dream +
        # _build_curators). Exposed for lifecycle tests + CUTOVER wiring.
        self.dream_engine: DreamEngine | None = None
        self._dream_interval: int = _DEFAULT_DREAM_INTERVAL
        self.curators: dict[str, ExperienceCurator] = {}
        self._curator_intervals: dict[str, int] = {}

        # Background-task bookkeeping.
        self._tasks: list[asyncio.Task[None]] = []
        self._stop_event: asyncio.Event = asyncio.Event()

    # ------------------------------------------------------------------
    # Build helpers (re-homed from Workspace)
    # ------------------------------------------------------------------

    def _maybe_build_dream(self) -> DreamEngine | None:
        """Build the workspace DreamEngine from the default pool's memory system.

        Re-homed verbatim from ``Workspace._maybe_build_dream``. Returns
        ``None`` when dream is disabled, there is no default pool, or the pool
        data has not been built yet.
        """
        name = self._default_pool_name
        if name is None:
            return None
        pool_cfg = self._pools_config.get(name)
        if pool_cfg is None or pool_cfg.memory is None:
            return None
        dream_cfg = pool_cfg.memory.dream_engine
        if dream_cfg is None or not dream_cfg.enabled:
            return None

        pool_data = self._pool_data.get(name)
        if pool_data is None:
            return None
        memory_system = pool_data.context_manager.memory_system
        archive_manager = memory_system.archive_manager
        knowledge_manager = memory_system.knowledge_manager
        if archive_manager is None or knowledge_manager is None:
            return None

        self._dream_interval = dream_cfg.interval
        engine = DreamEngine(
            history_manager=archive_manager,
            long_term_manager=knowledge_manager,
            registry=memory_system.store_registry,
            max_consume_per_run=dream_cfg.max_consume_per_run,
            consolidator=memory_system.knowledge_consolidator,
        )
        self.dream_engine = engine
        logger.info(
            "Workspace DreamEngine initialized, pool=%s, interval=%ds",
            name,
            self._dream_interval,
        )
        return engine

    def _build_curators(self) -> None:
        """Create one :class:`ExperienceCurator` per pool whose main agent
        enables experience, bound to that pool's already-built ``pool_data``.

        Re-homed verbatim from ``Workspace._build_curators``. Idempotent: pools
        already in :attr:`curators` are skipped.
        """
        for pool_name, pool_cfg in self._pools_config.items():
            if pool_name in self.curators:
                continue
            main_cfg = next(
                (a for a in pool_cfg.agents if a.role == "main"), None
            )
            if (
                main_cfg is None
                or main_cfg.experience is None
                or not main_cfg.experience.enabled
            ):
                continue
            pool_data = self._pool_data.get(pool_name)
            if pool_data is None:
                # curator requires the experience dir / meta from pool_data;
                # skip pools that have not been built yet.
                continue
            curator = ExperienceCurator(
                experience_dir=pool_data.experience_dir,
                meta_store=pool_data.experience_meta,
                max_experiences=main_cfg.experience.max_experiences,
            )
            self.curators[pool_name] = curator
            self._curator_intervals[pool_name] = (
                main_cfg.experience.curator_interval
            )
            logger.info(
                "Workspace ExperienceCurator initialized, pool=%s, interval=%ds",
                pool_name,
                main_cfg.experience.curator_interval,
            )

    # ------------------------------------------------------------------
    # Lifecycle (re-homed from Workspace)
    # ------------------------------------------------------------------

    @property
    def tasks(self) -> list[asyncio.Task[None]]:
        """Read-only view of currently running background tasks."""
        return self._tasks

    async def start(self) -> None:
        """Build (if needed) and launch the dream + curator background loops.

        Re-homed verbatim from ``Workspace.start_background_tasks``. Guards
        against double-start: if tasks are already running this is a no-op.
        """
        if self._tasks:
            return  # already started

        if self.dream_engine is None:
            self._maybe_build_dream()
        self._build_curators()

        self._stop_event.clear()

        if self.dream_engine is not None:
            self._tasks.append(
                asyncio.create_task(
                    self._dream_loop(self._dream_interval),
                    name="workspace-dream",
                )
            )
        for pool_name, curator in self.curators.items():
            interval = self._curator_intervals.get(
                pool_name, _DEFAULT_CURATOR_INTERVAL
            )
            self._tasks.append(
                asyncio.create_task(
                    self._curator_loop(curator, interval),
                    name=f"workspace-curator-{pool_name}",
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

    async def _curator_loop(
        self, curator: ExperienceCurator, interval: int
    ) -> None:
        """Periodically run ``curator.run`` until stopped.

        Re-homed verbatim from ``Workspace._curator_background_loop``.
        """
        while await self._wait_tick(interval):
            try:
                result = await curator.run()
                logger.info(
                    "Workspace ExperienceCurator: checked=%d evicted=%d",
                    result.get("checked", 0),
                    result.get("evicted", 0),
                )
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception(
                    "Workspace ExperienceCurator background loop error"
                )
