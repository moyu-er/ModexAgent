"""SubagentPool — LRU instance reuse for dynamic subagents.

Framework-layer abstraction.  ``send_to_agent`` to a subagent type routes
through ``SubagentPool.acquire()`` which returns or creates an instance.
Session isolation via ``session_id`` ensures no cross-task contamination.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from framework.multi_agent.registry import AgentInstance

logger = logging.getLogger(__name__)


@dataclass
class _PoolEntry:
    instance: "AgentInstance"
    created_at: float
    last_used: float


class SubagentPool:
    """LRU pool for subagent instance reuse.

    ``acquire(agent_type, factory)`` returns an existing instance or creates
    one via ``factory()``.  Idle instances are evicted after ``ttl_seconds``.
    """

    def __init__(
        self,
        max_size: int = 8,
        ttl_seconds: float = 1800.0,
        eviction_check_interval: float = 120.0,
    ) -> None:
        self._pool: dict[str, _PoolEntry] = {}  # key = agent_type
        self._lru_order: list[str] = []
        self._max_size = max_size
        self._ttl = ttl_seconds
        self._eviction_interval = eviction_check_interval
        self._lock = asyncio.Lock()
        self._cleanup_task: asyncio.Task[None] | None = None
        self._closed = False

    # -- public API -----------------------------------------------------------

    async def acquire(
        self,
        agent_type: str,
        factory: Callable[[], Awaitable["AgentInstance"]],
    ) -> "AgentInstance":
        """Get or create a subagent instance for ``agent_type``.

        ``factory`` is called only on cache miss.
        """
        async with self._lock:
            if agent_type in self._pool:
                entry = self._pool[agent_type]
                entry.last_used = time.monotonic()
                self._touch_lru(agent_type)
                logger.debug("SubagentPool: hit %s", agent_type)
                return entry.instance

            # Evict oldest if full
            while len(self._pool) >= self._max_size:
                oldest = self._lru_order[0]
                await self._evict(oldest)

            logger.info("SubagentPool: creating %s (miss)", agent_type)
            instance = await factory()
            self._pool[agent_type] = _PoolEntry(
                instance=instance,
                created_at=time.monotonic(),
                last_used=time.monotonic(),
            )
            self._lru_order.append(agent_type)
            return instance

    async def evict(self, agent_type: str) -> None:
        """Evict a specific agent type from the pool."""
        async with self._lock:
            await self._evict(agent_type)

    async def start_cleanup(self) -> None:
        """Start background TTL eviction task."""
        if self._cleanup_task is not None:
            return
        self._cleanup_task = asyncio.create_task(self._cleanup_stale_loop())

    async def close(self) -> None:
        """Shut down pool: cancel cleanup, evict all."""
        self._closed = True
        if self._cleanup_task is not None:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
        async with self._lock:
            for agent_type in list(self._pool.keys()):
                await self._evict(agent_type)

    @property
    def size(self) -> int:
        return len(self._pool)

    @property
    def cached_types(self) -> list[str]:
        return list(self._pool.keys())

    # -- internal -------------------------------------------------------------

    def _touch_lru(self, agent_type: str) -> None:
        """Move agent_type to end of LRU order."""
        if agent_type in self._lru_order:
            self._lru_order.remove(agent_type)
        self._lru_order.append(agent_type)

    async def _evict(self, agent_type: str) -> None:
        """Internal eviction without lock (caller holds lock)."""
        entry = self._pool.pop(agent_type, None)
        if agent_type in self._lru_order:
            self._lru_order.remove(agent_type)
        if entry is not None:
            try:
                instance = entry.instance
                if hasattr(instance, "pipeline") and instance.pipeline is not None:
                    await instance.pipeline.shutdown()
            except Exception:
                logger.exception("SubagentPool: error shutting down %s", agent_type)

    async def _cleanup_stale_loop(self) -> None:
        """Periodic TTL-based eviction of idle instances."""
        while not self._closed:
            try:
                await asyncio.sleep(self._eviction_interval)
                await self._cleanup_stale()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("SubagentPool: cleanup_stale loop error")

    async def _cleanup_stale(self) -> None:
        now = time.monotonic()
        async with self._lock:
            stale = [t for t in self._lru_order if now - self._pool[t].last_used > self._ttl]
        for agent_type in stale:
            logger.info(
                "SubagentPool: evicting stale %s (idle %.0fs)",
                agent_type,
                now - self._pool[agent_type].last_used,
            )
            async with self._lock:
                await self._evict(agent_type)
