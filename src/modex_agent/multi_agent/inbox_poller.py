"""InboxPoller — the sole between-turn driver for one pool (ADR-spec P3/P5).

Ticks every ``interval`` seconds; for each session with pending inbox input,
starts one drain cycle. Single-flight via ``inflight`` dict + try/finally pop +
per-tick reconcile. Lazy-materializes subagent instances on first turn. The
fold-in hook handles mid-turn consumption (P4).

Per-envelope turn execution (session tracking, InputMessage reconstruction,
``process_message``, session caps) is delegated to ``pool.dispatch_envelope``
so the poller stays thin and session/metadata locality stays on the pool.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING

from modex_agent.core.session_id import SessionInfo

if TYPE_CHECKING:
    from modex_agent.core.session_registry import SessionRegistry
    from modex_agent.multi_agent.descriptor import AgentInstance
    from modex_agent.multi_agent.pool import AgentPool
    from modex_agent.multi_agent.template import AgentTemplate

logger = logging.getLogger(__name__)


class InboxPoller:
    """One per pool. Owned by AgentPool; started/stopped with the pool."""

    def __init__(
        self,
        pool: AgentPool,
        *,
        interval: float = 0.2,
        session_registry: SessionRegistry | None = None,
    ) -> None:
        self._pool = pool
        self._interval = interval
        self._session_registry = session_registry or pool.session_registry
        self._inflight: dict[str, asyncio.Task[None]] = {}
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        for t in list(self._inflight.values()):
            t.cancel()
        await asyncio.gather(*self._inflight.values(), return_exceptions=True)
        self._inflight.clear()

    async def _loop(self) -> None:
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("InboxPoller tick crashed")
            await asyncio.sleep(self._interval)

    async def _tick(self) -> None:
        self._reconcile()
        for sid in await self._pool.sessions_with_pending():
            self._maybe_start(sid)

    def _reconcile(self) -> None:
        # Evict any done-but-leaked inflight entry (self-heals a missed
        # finally). A task cancelled by stop() is not a crash — don't log it.
        for sid in [s for s, t in self._inflight.items() if t.done()]:
            task = self._inflight[sid]
            if not task.cancelled():
                exc = task.exception()
                if exc is not None:
                    logger.error("Turn for %s crashed", sid, exc_info=exc)
            self._inflight.pop(sid, None)

    def _maybe_start(self, sid: str) -> None:
        existing = self._inflight.get(sid)
        if existing is not None and not existing.done():
            return  # busy → fold-in hook handles mid-turn
        info = SessionInfo.from_str(sid)
        agent = info.agent_name
        if not agent or agent == "unknown":
            return
        instance = self._pool.get(agent)
        if instance is None or instance.pipeline is None:
            template = self._pool.get_template(agent)
            if template is None:
                logger.error("InboxPoller: no template for %s; skipping", agent)
                return
            self._inflight[sid] = asyncio.create_task(self._materialize_then_turn(sid, template))
        else:
            self._inflight[sid] = asyncio.create_task(self._run_turn(sid, instance))

    async def _dispatch_batch(
        self, sid: str, instance: AgentInstance
    ) -> None:
        """Consume one batch and dispatch each envelope as its own turn.

        Per the agreed model: every envelope is a separate agent turn
        (between-turn dispatch is per-envelope); the fold-in hook does the
        mid-turn batch pull.
        """
        batch = await self._pool.consume_inbox(sid)
        for envelope in batch:
            await self._pool.dispatch_envelope(sid, instance, envelope)

    async def _run_turn(self, sid: str, instance: AgentInstance) -> None:
        try:
            await self._ensure_session_registered(sid)
            await self._dispatch_batch(sid, instance)
        finally:
            self._inflight.pop(sid, None)

    async def _ensure_session_registered(self, sid: str) -> None:
        """Register a session that is in the inbox but not yet in the registry.

        Generic helper: when a message arrives for a session id the local
        registry has never seen, create the session record before dispatching.
        The agent instance is expected to be already registered (eager main
        agents); this only ensures the session metadata exists.
        """
        if self._session_registry is None:
            return
        existing = await self._session_registry.get(sid)
        if existing is None:
            info = SessionInfo.from_str(sid)
            await self._session_registry.register(info)

    async def _materialize_then_turn(self, sid: str, template: AgentTemplate) -> None:
        try:
            await self._ensure_session_registered(sid)
            # Peek (non-destructive) the first pending envelope to read the
            # authoritative parent link — every envelope in a subagent inbox is
            # from the same parent. The batch is consumed only AFTER a
            # successful materialize, so a materialize failure still leaves the
            # messages in the inbox.
            peeked = await self._pool.peek_inbox(sid, limit=1)
            parent_sid = peeked[0].parent_session_id if peeked else None
            instance = await self._pool.materialize_agent(
                sid, template, parent_session_id=parent_sid
            )
            await self._dispatch_batch(sid, instance)
        except Exception:
            logger.exception("Materialize/turn failed for %s; message stays in inbox", sid)
        finally:
            self._inflight.pop(sid, None)
