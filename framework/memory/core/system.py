"""Application-facing memory system abstraction."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from framework.memory.archive_models import ArchiveChannel
from framework.memory.core.message import ChatMessage
from framework.memory.core.models import LongTermMemory
from framework.memory.core.scope import MemoryContext
from framework.memory.history import MessageHistory


class MemorySystem(ABC):
    """Abstract application-facing memory capability — core CRUD lifecycle.

    Prompt/context assembly belongs to memory injection policies, not this
    core CRUD/lifecycle contract.
    """

    @abstractmethod
    async def initialize(self) -> None:
        pass

    @abstractmethod
    async def close(self) -> None:
        pass

    @abstractmethod
    def create_message_history(
        self,
        context: MemoryContext,
        initial_messages: Sequence[ChatMessage | dict[str, Any]] | None = None,
    ) -> MessageHistory:
        pass

    @abstractmethod
    async def add_messages(
        self,
        context: MemoryContext,
        messages: Sequence[ChatMessage | dict[str, Any]],
    ) -> None:
        pass

    @abstractmethod
    async def get_history(
        self,
        context: MemoryContext,
        max_messages: int | None = None,
    ) -> list[ChatMessage]:
        pass

    @abstractmethod
    async def search(
        self,
        query: str,
        context: MemoryContext,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        pass

    @abstractmethod
    async def clear(self, context: MemoryContext) -> None:
        pass


class InjectableMemorySystem(ABC):
    """Read facade required by full memory injection policies.

    Separate from MemorySystem so that test fakes and injection-only
    consumers don't need to implement the full CRUD lifecycle.
    """

    @abstractmethod
    async def get_knowledge(self, context: MemoryContext) -> LongTermMemory:
        """Return all long-term knowledge for the given context."""
        ...

    @abstractmethod
    async def retrieve_knowledge(
        self,
        context: MemoryContext,
        query: str = "",
    ) -> LongTermMemory:
        """Retrieve knowledge relevant to a query."""
        ...

    @abstractmethod
    async def get_history_entries(
        self,
        context: MemoryContext,
        limit: int = 5,
        query: str = "",
        *,
        channel: ArchiveChannel = ArchiveChannel.CONTEXT,
    ) -> list[dict[str, Any]]:
        """Return recent archive entries for injection."""
        ...

    @abstractmethod
    def get_providers(self) -> list[Any]:
        """Return registered memory providers."""
        ...

    @abstractmethod
    async def prefetch_memories(self, query: str, context: MemoryContext) -> str | None:
        """Pre-fetch memories for the given query."""
        ...

    @abstractmethod
    async def get_knowledge_directory(self, context: MemoryContext) -> Path | None:
        """Return the knowledge storage directory."""
        ...

    @abstractmethod
    async def get_storage_path(self, context: MemoryContext) -> Path | None:
        """Return the storage path for the given context."""
        ...


class BudgetManagedMemorySystem(ABC):
    """Optional pre-load budget hook used by the context-manager bridge."""

    @abstractmethod
    async def ensure_within_budget(self, context: MemoryContext) -> None: ...


class ContextManagedMemorySystem(
    BudgetManagedMemorySystem,
    ABC,
):
    """Full memory capability expected by MemorySystemContextManager."""

    @abstractmethod
    def create_message_history(
        self,
        context: MemoryContext,
        initial_messages: Sequence[ChatMessage | dict[str, Any]] | None = None,
    ) -> MessageHistory: ...

    @abstractmethod
    async def add_messages(
        self,
        context: MemoryContext,
        messages: Sequence[ChatMessage | dict[str, Any]],
    ) -> None: ...

    @abstractmethod
    async def get_history(
        self,
        context: MemoryContext,
        max_messages: int | None = None,
    ) -> list[ChatMessage]: ...

    @abstractmethod
    async def clear(self, context: MemoryContext) -> None: ...
