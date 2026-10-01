"""BackendProvider — per-turn transport borrowing seam (ADR-0027, T2).

The :class:`BackendProvider` ABC decouples :class:`ExternalAgent` from
owning a fixed :class:`ExternalTransport`. The agent borrows a transport
per turn via :meth:`BackendProvider.acquire` and returns it via
:meth:`BackendProvider.release` whether the turn succeeded or failed. Pool
shutdown converges on :meth:`BackendProvider.close_all`.

:class:`PoolScopedBackendProvider` wraps a single pool-scoped transport,
reused across all turns. Both the main-agent and subagent external paths
use it — the :class:`OpenCodeServerManager` singleton handles process
lifecycle, and ``OpenCodeTransport.close()`` is a no-op.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from modex_agent.core.agent import ProviderKind

from .transports.abc import ExternalTransport

__all__ = [
    "BackendProvider",
    "PoolScopedBackendProvider",
    "TurnContext",
]


@dataclass(frozen=True)
class TurnContext:
    """Per-turn context passed to :meth:`BackendProvider.acquire`."""

    provider_kind: ProviderKind
    workdir: Path | None = None


class BackendProvider(ABC):
    """Borrowing seam for :class:`ExternalTransport` instances.

    The agent never owns a transport. Each turn opens with
    :meth:`acquire` and closes with :meth:`release` in a ``finally`` block.
    Pool shutdown converges on :meth:`close_all`.
    """

    @abstractmethod
    async def acquire(
        self, modex_session_id: str, turn_context: TurnContext
    ) -> ExternalTransport:
        raise NotImplementedError

    @abstractmethod
    async def release(self, backend: ExternalTransport, *, turn_failed: bool) -> None:
        raise NotImplementedError

    @abstractmethod
    async def close_all(self) -> None:
        raise NotImplementedError


class PoolScopedBackendProvider(BackendProvider):
    """Single pool-scoped transport, reused across all turns.

    Both main-agent and subagent external paths use this — the
    :class:`OpenCodeServerManager` singleton handles the shared process
    lifecycle; ``OpenCodeTransport.close()`` is a no-op.
    """

    def __init__(self, backend: ExternalTransport) -> None:
        self._backend = backend

    async def acquire(
        self, modex_session_id: str, turn_context: TurnContext
    ) -> ExternalTransport:
        return self._backend

    async def release(self, backend: ExternalTransport, *, turn_failed: bool) -> None:
        return

    async def close_all(self) -> None:
        await self._backend.close()
