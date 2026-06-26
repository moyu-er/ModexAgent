"""DreamScanner — periodic background scan that triggers DreamEngine consolidation.

Owns the background consolidation loop so the turn-execution core has no
background-loop concern mixed in. Duck-types the context manager's
``get_active_contexts`` + ``memory_system``; scopes a per-scope asyncio Lock
via the runtime dream-lock registry.
"""
from __future__ import annotations

import asyncio
import logging

from modex_agent.memory import MemoryContext
from modex_agent.memory.consolidation import DreamEngine
from modex_agent.runtime.dream_locks import _dream_locks

logger = logging.getLogger(__name__)


class DreamScanner:
    """Scan active contexts every ``dream_interval`` and trigger DreamEngine."""

    def __init__(
        self,
        dream_engine: DreamEngine,
        dream_interval: float,
        context_manager: object,
    ) -> None:
        self._dream_engine = dream_engine
        self._dream_interval = dream_interval
        self._context_manager = context_manager
        self._running = True

    def stop(self) -> None:
        self._running = False

    async def run_forever(self) -> None:
        """后台周期性扫描活跃 Context 并触发 DreamEngine。"""
        dream_engine = self._dream_engine
        dream_interval = self._dream_interval
        if dream_engine is None or dream_interval is None:
            return

        while self._running:
            try:
                await asyncio.sleep(dream_interval)
            except asyncio.CancelledError:
                break
            if not self._running:
                break
            # Duck typing: MemorySystemContextManager provides get_active_contexts + memory_system
            get_active = getattr(self._context_manager, "get_active_contexts", None)
            memory_system = getattr(self._context_manager, "memory_system", None)
            if get_active is None or memory_system is None:
                continue
            for ctx in get_active():
                try:
                    count = await memory_system.get_unprocessed_history_count(ctx)
                except Exception as scan_err:
                    logger.debug("DreamEngine scan error for %s: %s", str(ctx.session_id), scan_err)
                    continue
                if count > 0:
                    scope_key = f"{str(ctx.session_id) if ctx.session_id else ''}:{ctx.user_id or ''}:{ctx.tenant_id or ''}"
                    lock = _dream_locks.setdefault(scope_key, asyncio.Lock())

                    logger.info(
                        "DreamEngine timer trigger, scope=%s, count=%d",
                        scope_key,
                        count,
                    )

                    async def _run_dream(
                        c: MemoryContext = ctx,
                        engine: DreamEngine = dream_engine,
                        lk: asyncio.Lock = lock,
                    ) -> None:
                        async with lk:
                            try:
                                await engine.run(c)
                            except Exception as dream_err:
                                logger.warning("DreamEngine failed: %s", dream_err)

                    asyncio.create_task(_run_dream())
