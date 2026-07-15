"""Registry abstraction for resolving scoped memory stores."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Collection

from modex_agent.core.scope import (
    MemoryAgentRole,
    MemoryContext,
    MemoryLayerName,
    Scope,
    ScopeRecord,
)
from modex_agent.memory.core.split_stores import MemoryStoreBundle


class MemoryStoreRegistry(ABC):
    """Resolve a memory layer and scope into a :class:`MemoryStoreBundle`."""

    @abstractmethod
    async def initialize(self) -> None:
        pass

    @abstractmethod
    async def close(self) -> None:
        pass

    @abstractmethod
    async def resolve(
        self,
        *,
        layer: MemoryLayerName,
        scope: Scope,
        context: MemoryContext,
    ) -> MemoryStoreBundle:
        pass

    @abstractmethod
    async def list_records(
        self,
        *,
        layer: MemoryLayerName | None = None,
        agent_roles: Collection[str | MemoryAgentRole] | None = frozenset({MemoryAgentRole.MAIN}),
        has_file: str | None = None,
    ) -> list[ScopeRecord]:
        pass

    @abstractmethod
    async def evict(
        self,
        *,
        layer: MemoryLayerName | None = None,
        scope: Scope | None = None,
    ) -> None:
        pass
