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
import logging
from typing import TYPE_CHECKING

from modex_agent.core.session_id import session_id_prefix_of

if TYPE_CHECKING:
    from modex_agent.multi_agent.pool import AgentPool

logger = logging.getLogger(__name__)


class InboxPoller:
    """One per pool. Owned by AgentPool; started/stopped with the pool."""

    def __init__(self, pool: "AgentPool", *, interval: float = 0.2) -> None:
        self._pool = pool
        self._interval = interval
        self._inflight: dict[str, asyncio.Task[None]] = {}
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
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
        for sid in [s for s, t in self._inflight.items() if t.done()]:
            exc = self._inflight[sid].exception()
            if exc is not None:
                logger.error("Turn for %s crashed", sid, exc_info=exc)
            self._inflight.pop(sid, None)

    def _maybe_start(self, sid: str) -> None:
        existing = self._inflight.get(sid)
        if existing is not None and not existing.done():
            return  # busy → fold-in hook handles mid-turn
        from modex_agent.core.session_id import SessionInfo
        info = SessionInfo.from_str(sid)
        agent = info.agent_name
        if not agent or agent == "unknown":
            return
        instance = self._pool.get(agent)
        if instance is None or getattr(instance, "pipeline", None) is None:
            template = self._pool.get_template(agent)
            if template is None:
                logger.error("InboxPoller: no template for %s; skipping", agent)
                return
            self._inflight[sid] = asyncio.create_task(self._materialize_then_turn(sid, template))
        else:
            self._inflight[sid] = asyncio.create_task(self._run_turn(sid, instance))

    async def _run_turn(self, sid: str, instance: object) -> None:
        try:
            batch = await self._pool.consume_inbox(sid)
            for envelope in batch:  # one process_message per envelope (C1/C5)
                await self._pool.dispatch_envelope(sid, instance, envelope)
        finally:
            self._inflight.pop(sid, None)

    async def _materialize_then_turn(self, sid: str, template: object) -> None:
        try:
            parent = await self._pool.recover_parent_session(sid)
            inv_id = session_id_prefix_of(sid)
            instance = await template.materialize(parent, inv_id, self._pool._materialize_deps)
            batch = await self._pool.consume_inbox(sid)
            for envelope in batch:
                await self._pool.dispatch_envelope(sid, instance, envelope)
        except Exception:
            logger.exception("Materialize/turn failed for %s; message stays in inbox", sid)
        finally:
            self._inflight.pop(sid, None)
