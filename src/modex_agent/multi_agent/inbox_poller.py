"""InboxPoller — the sole between-turn driver for one pool (ADR-spec P3/P5).

Event-driven with tick fallback. The main loop awaits a pool-level wakeup
``Event`` (``signal_wakeup``) with a ``timeout == interval`` so an unsignalled
poller still ticks as a defensive fallback. Wakeup is wired at the single
convergence point of all inbox writers — ``LocalAgentMessageBus.send`` — so
every path (user input, agent-to-agent, CLI ``modexctl send``, external
peer reply) reaches the poller in-process with ~zero latency.

Concurrency invariants (verified by ``test_inbox_poller_events.py``):

1. **Per-session single-flight, cross-session concurrency.** ``_maybe_start``
   skips a session whose turn is still in-flight (``_inflight[sid]``); other
   sessions keep starting turns. Mid-turn messages for the busy session are
   consumed by the fold-in hook (``InboxFlushHook``), not by a second turn.
2. **Wakeup is a coarse "please scan" signal, not a per-message channel.**
   ``_tick`` re-scans the whole pool via ``sessions_with_pending()`` every
   time, so a wakeup that races with an in-progress tick is simply absorbed —
   the next tick sees whatever is pending. Messages are never lost.
3. **A turn finishing re-signals wakeup.** ``_run_turn`` /
   ``_materialize_then_turn`` call ``signal_wakeup`` in their ``finally``
   block so a message that arrived during the busy window is picked up
   immediately when the turn ends, instead of waiting up to one ``interval``.
   This closes the only behavioural gap between polling and event-driven modes.
4. **No busy-loop.** ``signal_wakeup`` only ``set``s a level-triggered
   ``Event``; the loop ``clear``s it once *before* each ``_tick`` so a signal
   set during the tick survives to wake the next wait, and N rapid sets still
   collapse into one rescan (a boolean can't accumulate).

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
    from modex_agent.multi_agent.session_tree.manager import SessionTreeManager
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
        # ``interval`` is the FALLBACK tick cadence, not the steady-state
        # latency. Under the event-driven path the poller wakes within
        # milliseconds of a ``bus.send``; this timer only covers writers that
        # bypass the bus (direct server writes, the pre-wiring window, future
        # out-of-process MQ backends). 0.2s bounds the worst-case latency for
        # those paths at a negligible cost (5 idle scans/s on an empty pool).
        # Tighten only if profiling shows the fallback is never needed; loosen
        # (e.g. 1-2s) if idle-scan cost ever matters.
        self._pool = pool
        self._interval = interval
        self._session_registry = session_registry or pool.session_registry
        self._inflight: dict[str, asyncio.Task[None]] = {}
        self._orphan_logged: set[str] = set()
        self._task: asyncio.Task[None] | None = None
        self._tree_manager: SessionTreeManager | None = None
        # Pool-level wakeup signal. Set by ``signal_wakeup`` (bus writers,
        # turn-completion finally), awaited in ``_loop``. Cleared once before
        # each ``_tick`` so a signal set DURING the tick survives to wake the
        # next wait (see ``_loop`` for the race rationale).
        self._wakeup_event: asyncio.Event = asyncio.Event()

    def signal_wakeup(self) -> None:
        """Signal that new inbox work may be pending.

        Called by ``LocalAgentMessageBus.send`` after a successful persist and
        by each turn's ``finally`` block. Idempotent: setting an already-set
        ``Event`` is a no-op. Safe to call from any coroutine in the loop.
        """
        self._wakeup_event.set()

    def attach_tree_manager(self, tree_manager: SessionTreeManager) -> None:
        self._tree_manager = tree_manager

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
            # Clear BEFORE the tick: any wakeup set DURING this tick (e.g. a
            # message arriving for a different session while _tick is spawning
            # a turn) must survive and wake the next wait immediately. Clearing
            # after the tick would swallow such in-flight signals, degrading to
            # the ``interval`` fallback latency.
            self._wakeup_event.clear()
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("InboxPoller tick crashed")
            # ``timeout=interval`` is the defensive fallback: an unsignalled
            # poller still rescans every ``interval`` (covering writers that
            # bypass the bus). A timeout is the expected fallback path, not an
            # error — suppress it and loop into the next tick.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wakeup_event.wait(), timeout=self._interval)

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
        if not agent:
            return
        instance = self._pool.get(agent)
        if instance is None or instance.pipeline is None:
            template = self._pool.get_template(agent)
            if template is None:
                if sid not in self._orphan_logged:
                    logger.error(
                        "InboxPoller: no template for %s; skipping session %s "
                        "(message stays pending — no silent drop per ADR-0015). "
                        "This usually means the message's agent_name does not "
                        "belong to this pool; check PoolRouter routing.",
                        agent,
                        sid,
                    )
                    self._orphan_logged.add(sid)
                return
            self._inflight[sid] = asyncio.create_task(self._materialize_then_turn(sid, template))
        else:
            self._inflight[sid] = asyncio.create_task(self._run_turn(sid, instance))

    async def _dispatch_batch(self, sid: str, instance: AgentInstance) -> None:
        """Consume one batch and dispatch each envelope as its own turn.

        The two inbox consumers divide labour by session state, NOT by message
        type:

        - **Poller (this path)** owns an *idle* session's entire pending batch:
          ``consume_inbox`` pulls all types (no ``only_types`` filter) and each
          envelope becomes its own between-turn. So a fold-eligible message
          (e.g. ``AGENT_MESSAGE``) reaching an idle session is delivered as a
          fresh turn here.
        - **InboxFlushHook (fold-in)** owns a *busy* session: it pulls only
          ``fold_eligible`` types (``EXTERNAL_INPUT`` excluded) into the running
          turn's history. It only fires while a turn is in-flight, so it never
          races this path — single-flight guarantees the poller skips a busy
          session before it could consume.

        Because ``consume`` is destructive and the two paths are mutually
        exclusive in time (idle vs busy), a given message is consumed exactly
        once by exactly one of them.
        """
        if self._tree_manager is not None:
            await self._tree_manager.on_dispatch_start(sid)
        batch = await self._pool.consume_inbox(sid)
        for envelope in batch:
            await self._pool.dispatch_envelope(sid, instance, envelope)

    async def _end_dispatch(self, sid: str) -> None:
        try:
            if self._tree_manager is not None:
                await self._tree_manager.on_dispatch_end(sid)
        except Exception:
            logger.exception("on_dispatch_end failed for %s", sid)
        finally:
            self._inflight.pop(sid, None)
            self.signal_wakeup()

    async def _run_turn(self, sid: str, instance: AgentInstance) -> None:
        try:
            await self._ensure_session_registered(sid)
            await self._dispatch_batch(sid, instance)
        finally:
            await self._end_dispatch(sid)

    async def _ensure_session_registered(
        self, sid: str, *, parent_session_id: str | None = None
    ) -> None:
        """Register a session that is in the inbox but not yet in the registry.

        Called from ``_materialize_then_turn`` (subagent lazy-materialization)
        and ``_run_turn`` (main agent, parent_session_id=None). Creates the
        session record before dispatching.

        ``parent_session_id`` is threaded in from the peeked envelope so the
        session is registered with the correct parent link in ONE step —
        eliminating the parentless window the WebUI could observe between
        a parent-less first registration and a parent-merge re-registration.
        """
        if self._session_registry is None:
            return
        existing = await self._session_registry.get(sid)
        if existing is None:
            info = SessionInfo.from_str(sid)
            if parent_session_id is not None:
                info = info.model_copy(update={"parent_session_id": parent_session_id})
            await self._session_registry.register(info)

    async def _materialize_then_turn(self, sid: str, template: AgentTemplate) -> None:
        try:
            # Peek (non-destructive) the first pending envelope to read the
            # authoritative parent link BEFORE registering — every envelope in a
            # subagent inbox is from the same parent. The batch is consumed only
            # AFTER a successful materialize, so a materialize failure still
            # leaves the messages in the inbox.
            #
            # Registering with the parent in ONE step eliminates the parentless
            # window where the WebUI could read the session with
            # parent_session_id = None (making a subagent appear as a main
            # agent). The old code registered without parent first, then
            # re-registered with parent — a race the WebUI could observe.
            peeked = await self._pool.peek_inbox(sid, limit=1)
            parent_sid = peeked[0].parent_session_id if peeked else None
            await self._ensure_session_registered(sid, parent_session_id=parent_sid)
            instance = await self._pool.materialize_agent(
                sid, template, parent_session_id=parent_sid
            )
            await self._dispatch_batch(sid, instance)
        except Exception:
            logger.exception("Materialize/turn failed for %s; message stays in inbox", sid)
        finally:
            await self._end_dispatch(sid)
