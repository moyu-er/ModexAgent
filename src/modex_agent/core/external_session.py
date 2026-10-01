"""External provider-session map contract.

The :class:`ExternalSessionMapStore` ABC sank to core (W3b): the persistence
domain implements it (SQLite) and the external-agent harness implements it
(local file), so the seam lives below both. The local-file implementation
stays in :mod:`modex_agent.agents.external.session_store`; the SQLite
implementation in :mod:`modex_agent.persistence.adapters.external_session_map_store`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from modex_agent.core.agent import ProviderKind

__all__ = ["ExternalSessionMapStore"]


class ExternalSessionMapStore(ABC):
    """Persistence seam for Modex-to-provider session mappings."""

    @abstractmethod
    def resolve(self, modex_session_id: str) -> tuple[str | None, bool]:
        """Resolve a resumable provider session for a Modex session."""

    @abstractmethod
    async def commit(
        self,
        modex_session_id: str,
        provider_session_id: str,
        provider_kind: ProviderKind,
    ) -> None:
        """Persist or replace a provider session mapping."""

    @abstractmethod
    async def invalidate(self, modex_session_id: str) -> None:
        """Prevent a provider session mapping from being resumed."""
