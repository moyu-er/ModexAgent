"""Typed memory layer capabilities and containers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from framework.memory.archive_models import (
    ArchiveBundleResult,
    ArchiveChannel,
    ArchiveWrite,
)
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
    async def get_recent_messages(
        self,
        context: MemoryContext,
        limit: int | None = None,
    ) -> list[ChatMessage]:
        """Return the most recent messages, up to *limit*.

        This is a **windowed view** intended for LLM context injection and
        external read APIs that need bounded output.  When *limit* is None,
        the implementation's configured ``max_messages`` is used.

        For operations that require the **complete** message list — compression
        triggers, archiving, summarisation, decision-making — use
        ``get_all_messages()`` instead.
        """
        pass

    @abstractmethod
    async def get_all_messages(self, context: MemoryContext) -> list[ChatMessage]:
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
        idle_threshold_seconds: float | None = None,
    ) -> StorageRevision | None:
        pass

    @abstractmethod
    async def get_revision(self, context: MemoryContext) -> StorageRevision:
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

    async def append_bundle(
        self,
        context: MemoryContext,
        writes: Sequence[ArchiveWrite],
    ) -> ArchiveBundleResult:
        raise NotImplementedError

    @abstractmethod
    async def get_recent(
        self,
        context: MemoryContext,
        limit: int = 5,
        *,
        channel: ArchiveChannel = ArchiveChannel.CONTEXT,
    ) -> list[ArchiveEntry]:
        pass

    @abstractmethod
    async def search(
        self,
        context: MemoryContext,
        query: str,
        limit: int = 5,
        *,
        channel: ArchiveChannel = ArchiveChannel.CONTEXT,
    ) -> list[ArchiveEntry]:
        pass

    @abstractmethod
    async def get_unprocessed(
        self,
        context: MemoryContext,
        cursor_name: str,
        limit: int = 100,
        *,
        channel: ArchiveChannel = ArchiveChannel.KNOWLEDGE,
    ) -> UnprocessedResult:
        pass

    @abstractmethod
    async def commit_cursor(
        self,
        context: MemoryContext,
        cursor_name: str,
        cursor: int,
        *,
        channel: ArchiveChannel = ArchiveChannel.KNOWLEDGE,
    ) -> None:
        pass

    async def prune_consumed_pairs(self, context: MemoryContext) -> None:
        _ = context
        return None

    @abstractmethod
    async def clear(self, context: MemoryContext) -> None:
        pass

    async def get_storage_path(self, context: MemoryContext) -> Path | None:
        """Return the absolute filesystem path to the archive storage directory.

        Default returns None.  Subclasses backed by file storage should
        override to return the resolved storage directory.
        """
        _ = context
        return None

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

    async def get_storage_path(self, context: MemoryContext) -> Path | None:
        """Return the absolute path to knowledge storage, if file-backed."""
        return None

    @abstractmethod
    async def clear(self, context: MemoryContext) -> None:
        pass

    def get_scope(self) -> MemoryScope:
        """Return the scope used by this manager for storage resolution.

        Default is UserScope. Override when the manager is configured with
        a different scope (e.g. CompositeScope).
        """
        return UserScope()


class UserRetentionBuffer(ABC):
    """Auxiliary memory for pruned unfinished user/agent inputs."""

    @abstractmethod
    async def append_entries(
        self,
        context: MemoryContext,
        entries: Sequence[Any],
    ) -> None:
        pass

    @abstractmethod
    async def get_entries(self, context: MemoryContext) -> list[Any]:
        pass

    @abstractmethod
    async def replace_entries(
        self,
        context: MemoryContext,
        entries: Sequence[Any],
    ) -> None:
        pass

    @abstractmethod
    async def clear(self, context: MemoryContext) -> None:
        pass

    @abstractmethod
    async def mark_all_completed(
        self,
        context: MemoryContext,
        assistant_content: str,
    ) -> None:
        pass

    @abstractmethod
    async def upsert_pruned_user(
        self,
        context: MemoryContext,
        entry: Any,
    ) -> None:
        pass


@dataclass(frozen=True)
class MemoryLayerSet:
    """Fieldized memory layer ownership for the default tiered system."""

    session: SessionMemoryManager
    archive: ArchiveMemoryManager | None = None
    knowledge: KnowledgeMemoryManager | None = None
    user_retention: UserRetentionBuffer | None = None

    def with_session(self, manager: SessionMemoryManager) -> MemoryLayerSet:
        return replace(self, session=manager)

    def with_archive(self, manager: ArchiveMemoryManager | None) -> MemoryLayerSet:
        return replace(self, archive=manager)

    def with_knowledge(self, manager: KnowledgeMemoryManager | None) -> MemoryLayerSet:
        return replace(self, knowledge=manager)

    def with_user_retention(
        self,
        manager: UserRetentionBuffer | None,
    ) -> MemoryLayerSet:
        return replace(self, user_retention=manager)
