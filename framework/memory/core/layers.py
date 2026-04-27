"""Typed memory layer capabilities and containers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from framework.memory.core.consolidation import MemoryUpdate
from framework.memory.core.message import ChatMessage
from framework.memory.core.models import (
    ArchiveEntry,
    LongTermMemory,
    StorageRevision,
    UnprocessedResult,
)
from framework.memory.core.scope import MemoryContext, MemoryScope, UserScope


class SessionMemoryManager(ABC):
    """Live conversation memory layer capability."""

    @abstractmethod
    async def add_messages(
        self,
        context: MemoryContext,
        messages: Sequence[ChatMessage | dict[str, Any]],
    ) -> StorageRevision:
        pass

    @abstractmethod
    async def get_visible_messages(
        self,
        context: MemoryContext,
        limit: int | None = None,
    ) -> list[ChatMessage]:
        pass

    @abstractmethod
    async def get_all_messages(self, context: MemoryContext) -> list[ChatMessage]:
        pass

    @abstractmethod
    async def save_checkpoint(
        self,
        context: MemoryContext,
        messages: Sequence[ChatMessage | dict[str, Any]],
    ) -> None:
        pass

    @abstractmethod
    async def load_checkpoint(self, context: MemoryContext) -> list[ChatMessage] | None:
        pass

    @abstractmethod
    async def clear(self, context: MemoryContext) -> None:
        pass

    @abstractmethod
    async def replace_messages(
        self,
        context: MemoryContext,
        messages: Sequence[ChatMessage | dict[str, Any]],
    ) -> StorageRevision:
        pass

    @abstractmethod
    async def replace_messages_if_revision(
        self,
        context: MemoryContext,
        messages: Sequence[ChatMessage | dict[str, Any]],
        expected_revision: StorageRevision,
        state_updates: Mapping[str, Any] | None = None,
    ) -> StorageRevision | None:
        pass

    @abstractmethod
    async def get_revision(self, context: MemoryContext) -> StorageRevision:
        pass

    @abstractmethod
    async def get_checkpoint_id(self, context: MemoryContext) -> str | None:
        """Return the checkpoint ID stored for this session, if any."""
        pass

    @abstractmethod
    async def clear_checkpoint(self, context: MemoryContext) -> None:
        """Remove checkpoint data for this session without touching message history."""
        pass

    async def transform_messages(
        self,
        context: MemoryContext,
        transform: Callable[[list[ChatMessage]], list[ChatMessage]],
    ) -> StorageRevision | None:
        """Atomic transform: read, apply sync transform, write with revision check.

        Default implementation uses get_revision + get_all_messages +
        replace_messages_if_revision.  Subclasses may override for a more
        efficient in-lock transform.
        """
        import copy
        revision = await self.get_revision(context)
        messages = await self.get_all_messages(context)
        result = transform(copy.deepcopy(messages))
        return await self.replace_messages_if_revision(context, result, revision)

    async def get_state(self, context: MemoryContext, key: str) -> Any | None:
        """Read arbitrary state from session storage. Default no-op."""
        _ = context, key
        return None

    async def set_state(self, context: MemoryContext, key: str, value: Any) -> None:
        """Write arbitrary state to session storage. Default no-op."""
        _ = context, key, value
        pass


class ArchiveMemoryManager(ABC):
    """Compressed history/archive memory layer capability."""

    @abstractmethod
    async def append(self, context: MemoryContext, entry: ArchiveEntry) -> ArchiveEntry:
        pass

    @abstractmethod
    async def get_recent(self, context: MemoryContext, limit: int = 5) -> list[ArchiveEntry]:
        pass

    @abstractmethod
    async def search(
        self,
        context: MemoryContext,
        query: str,
        limit: int = 5,
    ) -> list[ArchiveEntry]:
        pass

    @abstractmethod
    async def get_unprocessed(
        self,
        context: MemoryContext,
        cursor_name: str,
        limit: int = 100,
    ) -> UnprocessedResult:
        pass

    @abstractmethod
    async def commit_cursor(
        self,
        context: MemoryContext,
        cursor_name: str,
        cursor: int,
    ) -> None:
        pass

    @abstractmethod
    async def clear(self, context: MemoryContext) -> None:
        pass

    def get_scope(self) -> MemoryScope:
        """Return the scope used by this manager for storage resolution.

        Default is UserScope. Override when the manager is configured with
        a different scope (e.g. CompositeScope).
        """
        return UserScope()


class KnowledgeMemoryManager(ABC):
    """Durable distilled knowledge memory layer capability."""

    @abstractmethod
    async def get_all(self, context: MemoryContext) -> LongTermMemory:
        pass

    async def retrieve(self, context: MemoryContext, query: str = "") -> LongTermMemory:
        """Retrieve knowledge for injection.

        Default knowledge storage is small, curated profile/fact content, so
        retrieval is intentionally all-content.  Query-aware retrieval remains
        an override point for custom managers.
        """
        _ = query
        return await self.get_all(context)

    @abstractmethod
    async def get_file(self, context: MemoryContext, file_key: str) -> str | None:
        pass

    @abstractmethod
    async def apply_update(self, context: MemoryContext, update: MemoryUpdate) -> str:
        pass

    @abstractmethod
    async def ensure_defaults(
        self,
        context: MemoryContext,
        defaults: Mapping[str, str] | None = None,
    ) -> None:
        pass

    @abstractmethod
    async def clear(self, context: MemoryContext) -> None:
        pass

    def get_scope(self) -> MemoryScope:
        """Return the scope used by this manager for storage resolution.

        Default is UserScope. Override when the manager is configured with
        a different scope (e.g. CompositeScope).
        """
        return UserScope()


@dataclass(frozen=True)
class MemoryLayerSet:
    """Fieldized memory layer ownership for the default tiered system."""

    session: SessionMemoryManager
    archive: ArchiveMemoryManager | None = None
    knowledge: KnowledgeMemoryManager | None = None

    def with_session(self, manager: SessionMemoryManager) -> MemoryLayerSet:
        return replace(self, session=manager)

    def with_archive(self, manager: ArchiveMemoryManager | None) -> MemoryLayerSet:
        return replace(self, archive=manager)

    def with_knowledge(self, manager: KnowledgeMemoryManager | None) -> MemoryLayerSet:
        return replace(self, knowledge=manager)
