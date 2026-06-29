"""Application-facing memory system abstraction."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from modex_agent.memory.archive_models import ArchiveChannel
from modex_agent.core.message import ChatMessage
from modex_agent.memory.core.models import LongTermMemory
from modex_agent.core.scope import MemoryContext
from modex_agent.memory.history import MessageHistory
from modex_agent.memory.pruned.manager import PrunedManager


class MemorySystem(ABC):
    """Abstract application-facing memory capability — CRUD lifecycle + injection reads.

    A complete memory system must implement both the lifecycle methods
    (initialize, close, CRUD) and the injection read methods (knowledge,
    archive, providers) used by injection policies.
    """

    # -- lifecycle ------------------------------------------------------------

    @abstractmethod
    async def initialize(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    # -- CRUD -----------------------------------------------------------------

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
    ) -> list[ChatMessage]: ...

    @abstractmethod
    async def search(
        self,
        query: str,
        context: MemoryContext,
        limit: int = 5,
    ) -> list[dict[str, Any]]: ...

    @abstractmethod
    async def clear(self, context: MemoryContext) -> None: ...

    # -- injection reads ------------------------------------------------------

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

    @property
    def pruned_manager(self) -> PrunedManager | None:
        """Pruned-message manager if configured; None by default."""
        return None


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
    ) -> list[ChatMessage]: ...

    @abstractmethod
    async def clear(self, context: MemoryContext) -> None: ...
