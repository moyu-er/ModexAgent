"""BackendProvider — per-turn backend borrowing seam (ADR-0027, T2 + T6).

The :class:`BackendProvider` ABC decouples :class:`ExternalCodingAgent` from
owning a fixed :class:`StreamingProviderBackend`. The agent borrows a backend
per turn via :meth:`BackendProvider.acquire` and returns it via
:meth:`BackendProvider.release` whether the turn succeeded or failed. Pool
shutdown converges on :meth:`BackendProvider.close_all`.

T2 shipped :class:`PoolScopedBackendProvider`, which wraps a single
pool-scoped backend for the main-agent external-coding path (ADR-0022).
Externally, main-agent behavior is byte-for-byte indistinguishable from
pre-ADR-0027: same backend reused across all turns, same warm SSE reuse,
same ``close()`` at pool shutdown.

T6 adds :class:`CachingBackendProvider` for the subagent path. It
maintains two caches:

- **Warm** (``OpenCodeServerBackend``): per-``modex_session_id``
  :class:`OrderedDict` with ``MAX_WARM_BACKENDS`` LRU cap. Each entry
  holds one long-lived ``opencode serve`` process. LRU eviction closes
  the evicted serve process so the total process count stays bounded
  regardless of how many modex sessions are active.
- **Stateless** (``OpenCodeBackend``, ``PiBackend``): shared single
  instance per ``provider_kind``. Per-turn subprocesses are auto-reaped
  on turn end; no LRU, no cap needed.

The business layer decides which backend class to instantiate and whether
the warm variant is in use; it implements :class:`BackendFactory` and
hands it to :class:`CachingBackendProvider` at construction.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from .paths import ProviderKind

if TYPE_CHECKING:
    # Annotation-only: avoids a runtime circular import (agent.py imports
    # TurnContext/BackendProvider from this module at runtime).
    from .agent import StreamingProviderBackend

__all__ = [
    "BackendFactory",
    "BackendProvider",
    "CachingBackendProvider",
    "PoolScopedBackendProvider",
    "TurnContext",
]


@dataclass(frozen=True)
class TurnContext:
    """Per-turn context passed to :meth:`BackendProvider.acquire`.

    Carries the information a provider may need to make a caching decision.
    :class:`PoolScopedBackendProvider` ignores it; the T6
    :class:`CachingBackendProvider` will use ``provider_kind`` to choose
    between warm and stateless backends. ``workdir`` mirrors the resolved
    per-turn workdir in case a future provider keys cache state by it.
    """

    provider_kind: ProviderKind
    workdir: Path | None = None


class BackendProvider(ABC):
    """Borrowing seam for :class:`StreamingProviderBackend` instances.

    The agent never owns a backend. Each turn opens with
    :meth:`acquire` and closes with :meth:`release` in a ``finally`` block.
    Pool shutdown converges on :meth:`close_all`, which replaces the old
    per-agent ``backend.close()`` call.
    """

    @abstractmethod
    async def acquire(
        self, modex_session_id: str, turn_context: TurnContext
    ) -> StreamingProviderBackend:
        """Return a backend for one turn.

        Implementations may return the same instance every turn
        (pool-scoped) or pick a fresh one based on ``turn_context``.
        """
        raise NotImplementedError

    @abstractmethod
    async def release(self, backend: StreamingProviderBackend, *, turn_failed: bool) -> None:
        """Return a borrowed backend.

        ``turn_failed=True`` indicates the turn raised before completing;
        providers may use this to invalidate cached state. The backend
        reference is passed back so a provider can correlate it with the
        instance returned by :meth:`acquire` without holding extra state.
        """
        raise NotImplementedError

    @abstractmethod
    async def close_all(self) -> None:
        """Release all backend resources (called once at pool shutdown)."""
        raise NotImplementedError


class PoolScopedBackendProvider(BackendProvider):
    """Main-agent path — single pool-scoped backend, reused across all turns.

    Wraps the pre-built backend so the agent borrows the same instance every
    turn. ``release`` is a no-op (the pool owns the lifetime); ``close_all``
    delegates to the wrapped backend's ``close()``. Externally
    indistinguishable from pre-ADR-0027 fixed-backend behavior.
    """

    def __init__(self, backend: StreamingProviderBackend) -> None:
        self._backend = backend

    async def acquire(
        self, modex_session_id: str, turn_context: TurnContext
    ) -> StreamingProviderBackend:
        return self._backend

    async def release(self, backend: StreamingProviderBackend, *, turn_failed: bool) -> None:
        # Pool-scoped: per-turn release is a no-op. The pool owns the
        # backend lifetime; close happens once via close_all().
        return

    async def close_all(self) -> None:
        await self._backend.close()


class BackendFactory(ABC):
    """Creates :class:`StreamingProviderBackend` instances on demand.

    The business layer implements this to decide which backend class to
    instantiate (``OpenCodeServerBackend`` vs ``OpenCodeBackend`` vs
    ``PiBackend``) and whether the warm variant is in use.
    :class:`CachingBackendProvider` calls :meth:`create` only on a cache
    miss and calls :meth:`is_warm` to pick the warm vs stateless cache.

    The factory itself is synchronous: instantiating a backend object
    does no I/O. The heavy work (subprocess spawn, SSE readiness) is
    deferred to the backend's ``execute_streaming`` so the provider can
    create the instance under its ``asyncio.Lock`` without nesting I/O.
    """

    @abstractmethod
    def create(self, provider_kind: ProviderKind) -> StreamingProviderBackend:
        """Return a fresh backend instance for ``provider_kind``."""
        raise NotImplementedError

    @abstractmethod
    def is_warm(self, provider_kind: ProviderKind) -> bool:
        """``True`` if ``provider_kind`` should use the warm cache path."""
        raise NotImplementedError


class CachingBackendProvider(BackendProvider):
    """Subagent path — provider-kind-aware backend caching.

    Warm backends (``OpenCodeServerBackend``): per-``modex_session_id``
    cache with ``MAX_WARM_BACKENDS`` LRU cap. Each entry holds one
    long-lived ``opencode serve`` process. LRU eviction closes the
    evicted serve process so the total process count stays bounded
    regardless of how many modex sessions are active.

    Stateless backends (``OpenCodeBackend``, ``PiBackend``): shared
    single instance per ``provider_kind``. Per-turn subprocesses are
    auto-reaped on turn end; no LRU, no cap needed.

    All cache dict access is guarded by an ``asyncio.Lock`` so concurrent
    turns from different modex sessions cannot corrupt the caches.
    """

    MAX_WARM_BACKENDS: ClassVar[int] = 10

    def __init__(self, backend_factory: BackendFactory) -> None:
        self._factory = backend_factory
        self._warm_backends: OrderedDict[str, StreamingProviderBackend] = OrderedDict()
        self._shared_backends: dict[ProviderKind, StreamingProviderBackend] = {}
        self._lock = asyncio.Lock()
        self._closed = False

    async def acquire(
        self, modex_session_id: str, turn_context: TurnContext
    ) -> StreamingProviderBackend:
        async with self._lock:
            if self._closed:
                raise RuntimeError("CachingBackendProvider is closed")
            provider_kind = turn_context.provider_kind
            if self._factory.is_warm(provider_kind):
                return await self._acquire_warm(modex_session_id, provider_kind)
            return self._acquire_stateless(provider_kind)

    async def _acquire_warm(
        self, modex_session_id: str, provider_kind: ProviderKind
    ) -> StreamingProviderBackend:
        cached = self._warm_backends.get(modex_session_id)
        # ``getattr(..., '_closed', False)`` is a real extension boundary:
        # the provider holds ``StreamingProviderBackend`` and cannot know
        # whether the concrete subclass exposes ``_closed`` (real
        # backends do; test doubles like ``ScriptedStreamingAdapter`` do
        # not). Rule 6 exception — documented here.
        if cached is not None and not getattr(cached, "_closed", False):
            self._warm_backends.move_to_end(modex_session_id)
            return cached
        while len(self._warm_backends) >= self.MAX_WARM_BACKENDS:
            _, evicted = self._warm_backends.popitem(last=False)
            await evicted.close()
        backend = self._factory.create(provider_kind)
        self._warm_backends[modex_session_id] = backend
        return backend

    def _acquire_stateless(self, provider_kind: ProviderKind) -> StreamingProviderBackend:
        cached = self._shared_backends.get(provider_kind)
        if cached is not None and not getattr(cached, "_closed", False):
            return cached
        backend = self._factory.create(provider_kind)
        self._shared_backends[provider_kind] = backend
        return backend

    async def release(self, backend: StreamingProviderBackend, *, turn_failed: bool) -> None:
        if not turn_failed:
            return
        async with self._lock:
            for sid, cached in list(self._warm_backends.items()):
                if cached is backend:
                    del self._warm_backends[sid]
                    await backend.close()
                    return
            # Stateless backends are shared across sessions — release is
            # a no-op even on turn_failed. The shared instance stays.

    async def close_all(self) -> None:
        async with self._lock:
            self._closed = True
            for backend in self._warm_backends.values():
                await backend.close()
            self._warm_backends.clear()
            for backend in self._shared_backends.values():
                await backend.close()
            self._shared_backends.clear()
