"""The ExperienceSupply — the experience capability's single lifecycle owner.

Owns the catalog (and its meta store + curator), the curator background
loop, and every review task in the pool (plan §10.5). The retired
hook-owned ``_pending`` task set died here: review submissions go through
``submit_review``, which accepts while running and rejects during stop —
so no review task can outlive supply teardown.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

from modex_agent.plugins.capability import CapabilitySupply
from modex_agent.plugins.defaults.capabilities.experience.catalog import ExperienceCatalog
from modex_agent.plugins.defaults.capabilities.experience.config import (
    ExperiencePoolConfig,
    ExperienceReviewConfig,
)
from modex_agent.plugins.defaults.capabilities.experience.metadata import (
    ExperienceMetaStore,
    PerFileExperienceMetaStore,
)

logger = logging.getLogger(__name__)


class ExperienceSupply(CapabilitySupply):
    """The pool-level supply (SPEC §8.3 supply row, plan §10.5 lifecycle).

    Lifecycle (pool assembly starts; both teardown roads stop):

    ``start()`` clears the stop flag, starts the curator loop, and allows
    review submissions. ``stop()`` flips to stopping (new submissions are
    rejected), cancels and awaits every pending review task, stops the
    curator, and closes owned resources. Both are idempotent.

    Regular class (NOT a frozen dataclass — rule 11/12): it holds live
    ``asyncio.Task`` objects and events; that mutable runtime state is the
    point of the lifecycle it owns.
    """

    def __init__(
        self,
        *,
        pool_name: str,
        catalog: ExperienceCatalog,
        experience_dir: Path,
        meta_store: ExperienceMetaStore,
        pool_config: ExperiencePoolConfig,
        review_config_by_agent: dict[str, ExperienceReviewConfig],
        review_provider: Any | None,
    ) -> None:
        self.pool_name = pool_name
        self.catalog = catalog
        self.experience_dir = experience_dir
        self.meta_store = meta_store
        self.pool_config = pool_config
        self.review_config_by_agent = review_config_by_agent
        self.review_provider = review_provider
        self._curator_interval = pool_config.curator_interval
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._stopping = False
        self._review_tasks: dict[str, asyncio.Task[None]] = {}
        self._review_agents: dict[str, Any] = {}

    # ── reviewer registration (hook_factory wires per-agent reviewers) ──

    def register_review_agent(self, agent_name: str, review_agent: Any) -> None:
        """Attach a built reviewer for one agent (its hook retrieves it).

        The reviewer is built by the HOOK-slot factory from this supply's
        ``review_provider``; registration keeps the supply the single
        lifecycle owner while the factory stays the construction path.
        """
        self._review_agents[agent_name] = review_agent

    def review_agent_for(self, agent_name: str) -> Any | None:
        """The agent's registered reviewer, or ``None`` (fail-soft skip)."""
        return self._review_agents.get(agent_name)

    # ── review submissions (the retired hook-owned task set died here) ──

    def review_in_flight(self, agent_name: str) -> bool:
        """Whether a review is currently running for the agent (mutex gate)."""
        task = self._review_tasks.get(agent_name)
        return task is not None and not task.done()

    def submit_review(
        self,
        *,
        agent_name: str,
        review_factory: Callable[[], Coroutine[Any, Any, None]],
        invocation_id: str,
    ) -> asyncio.Task[None] | None:
        """Build and submit one review coroutine for owned execution.

        Admission is synchronous and authoritative: a stopping supply or an
        agent with a live review rejects the factory before a coroutine exists.
        Returns the task while running, otherwise ``None``.
        """
        existing = self._review_tasks.get(agent_name)
        if self._stopping or (existing is not None and not existing.done()):
            logger.info(
                "ExperienceSupply: review submission rejected agent=%s "
                "invocation=%s stopping=%s in_flight=%s",
                agent_name,
                invocation_id,
                self._stopping,
                existing is not None and not existing.done(),
            )
            return None

        review = review_factory()
        try:
            task = asyncio.create_task(
                self._run_review(agent_name, review, invocation_id),
                name=f"exp-review-{agent_name}-{invocation_id[:8]}",
            )
        except BaseException:
            review.close()
            raise
        self._review_tasks[agent_name] = task
        task.add_done_callback(
            lambda completed: self._remove_review_task(agent_name, completed)
        )
        return task

    def _remove_review_task(
        self, agent_name: str, completed: asyncio.Task[None]
    ) -> None:
        if self._review_tasks.get(agent_name) is completed:
            self._review_tasks.pop(agent_name)

    async def _run_review(
        self,
        agent_name: str,
        review: Coroutine[Any, Any, None],
        invocation_id: str,
    ) -> None:
        try:
            await review
        except asyncio.CancelledError:
            logger.info(
                "ExperienceSupply: review cancelled agent=%s invocation=%s",
                agent_name,
                invocation_id,
            )
            raise
        except Exception:
            # Background review failure is isolated from the completed
            # foreground turn (invariant §10.6).
            logger.exception(
                "ExperienceSupply: review failed agent=%s invocation=%s",
                agent_name,
                invocation_id,
            )

    # ── D4 lifecycle: pool assembly starts; pool teardown stops ─────────

    async def start(self) -> None:
        """Start the curator background loop (idempotent).

        Called by pool assembly (:class:`PoolAssembleStage`) right after
        the supply aggregation — the object is never a running worker
        before the pool that owns it exists. Review submissions are
        allowed from this point.
        """
        if self._task is not None:
            return  # already started
        self._stopping = False
        self._stop_event.clear()
        self._task = asyncio.create_task(
            self._curator_loop(), name=f"experience-curator-{self.pool_name}"
        )
        logger.info(
            "Experience curator loop started, pool=%s, interval=%ds",
            self.pool_name,
            self._curator_interval,
        )

    async def stop(self) -> None:
        """Reject new submissions, cancel+await reviews, stop the curator.

        Idempotent; both teardown roads call this — the pipeline's
        cleanup-on-failure (registered by the stage) and
        ``AgentPool.shutdown_all`` — so nothing this supply started can
        leak.
        """
        self._stopping = True
        self._stop_event.set()

        # Cancel and await every pending review task first (§10.5 order).
        pending = [task for task in self._review_tasks.values() if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._review_tasks.clear()

        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    @property
    def task(self) -> asyncio.Task[None] | None:
        """The running curator loop task (``None`` while stopped) — test seam."""
        return self._task

    @property
    def stopping(self) -> bool:
        """Whether the supply is stopping (submissions rejected)."""
        return self._stopping

    # ── the loop ──────────────────────────────────────────────────────────

    async def _wait_tick(self, interval: int) -> bool:
        """Sleep *interval* seconds; ``False`` once stopped."""
        await asyncio.sleep(interval)
        return not self._stop_event.is_set()

    async def _curator_loop(self) -> None:
        """Periodically run ``curator.run`` until stopped (LRU eviction)."""
        while await self._wait_tick(self._curator_interval):
            try:
                result = await self.catalog.curate()
                logger.info(
                    "ExperienceCurator: pool=%s checked=%d evicted=%d",
                    self.pool_name,
                    result.checked,
                    result.evicted,
                )
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("ExperienceCurator background loop error")


def build_experience_supply(
    *,
    pool_name: str,
    data_dir: Path,
    root_agent_name: str,
    pool_config: ExperiencePoolConfig,
    review_config_by_agent: dict[str, ExperienceReviewConfig],
    review_provider: Any | None,
    experience_dir: Path,
) -> ExperienceSupply:
    """The capability ``supply()`` construction body (kept here so the
    capability module stays a thin protocol face)."""
    experience_dir.mkdir(parents=True, exist_ok=True)
    meta_store = PerFileExperienceMetaStore(lambda: experience_dir)
    catalog = ExperienceCatalog(
        experience_dir=experience_dir,
        meta_store=meta_store,
        max_experiences=pool_config.max_experiences,
    )
    return ExperienceSupply(
        pool_name=pool_name,
        catalog=catalog,
        experience_dir=experience_dir,
        meta_store=meta_store,
        pool_config=pool_config,
        review_config_by_agent=review_config_by_agent,
        review_provider=review_provider,
    )
