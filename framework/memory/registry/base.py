"""Registry abstraction for resolving scoped memory stores."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Collection

from framework.memory.core.scope import (
    MemoryAgentRole,
    MemoryContext,
    MemoryLayerName,
    MemoryScope,
    ScopeRecord,
)
from framework.memory.core.storage import MemoryStorage


class MemoryStoreRegistry(ABC):
    """Resolve a memory layer and scope into one scoped storage instance."""

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
        scope: MemoryScope,
        context: MemoryContext,
    ) -> MemoryStorage:
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
        scope: MemoryScope | None = None,
    ) -> None:
        pass
