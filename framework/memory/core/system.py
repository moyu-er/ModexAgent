"""Application-facing memory system abstraction."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from framework.memory.archive_models import ArchiveChannel
from framework.memory.core.message import ChatMessage
from framework.memory.core.models import LongTermMemory
from framework.memory.core.scope import MemoryContext
from framework.memory.history import MessageHistory


class MemorySystem(ABC):
    """Abstract application-facing memory capability.

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


@runtime_checkable
class InjectableMemorySystem(Protocol):
    """Read facade required by full memory injection policies."""

    def create_message_history(
        self,
        context: MemoryContext,
        initial_messages: Sequence[ChatMessage | dict[str, Any]] | None = None,
    ) -> MessageHistory: ...

    async def get_history(
        self,
        context: MemoryContext,
        max_messages: int | None = None,
    ) -> list[ChatMessage]: ...

    async def get_knowledge(self, context: MemoryContext) -> LongTermMemory: ...

    async def retrieve_knowledge(
        self,
        context: MemoryContext,
        query: str = "",
    ) -> LongTermMemory: ...

    async def get_history_entries(
        self,
        context: MemoryContext,
        limit: int = 5,
        query: str = "",
        *,
        channel: ArchiveChannel = ArchiveChannel.CONTEXT,
    ) -> list[dict[str, Any]]: ...

    def get_providers(self) -> list[Any]: ...

    async def prefetch_memories(self, query: str, context: MemoryContext) -> str | None: ...

    async def get_knowledge_directory(self, context: MemoryContext) -> Path | None: ...

    async def get_archive_directory(self, context: MemoryContext) -> Path | None: ...


class BudgetManagedMemorySystem(Protocol):
    """Optional pre-load budget hook used by the context-manager bridge."""

    async def ensure_within_budget(self, context: MemoryContext) -> None: ...


class ContextManagedMemorySystem(
    BudgetManagedMemorySystem,
    Protocol,
):
    """Full memory capability expected by MemorySystemContextManager."""

    def create_message_history(
        self,
        context: MemoryContext,
        initial_messages: Sequence[ChatMessage | dict[str, Any]] | None = None,
    ) -> MessageHistory: ...

    async def add_messages(
        self,
        context: MemoryContext,
        messages: Sequence[ChatMessage | dict[str, Any]],
    ) -> None: ...

    async def get_history(
        self,
        context: MemoryContext,
        max_messages: int | None = None,
    ) -> list[ChatMessage]: ...

    async def clear(self, context: MemoryContext) -> None: ...
