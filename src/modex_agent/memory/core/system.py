"""Application-facing memory system abstraction."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from modex_agent.core import MessageHistory
from modex_agent.core.capabilities import ModelInfo
from modex_agent.core.message import ChatMessage
from modex_agent.memory.archive_models import ArchiveChannel
from modex_agent.memory.budget import ContextBudget
from modex_agent.memory.cleanup import CleanupResult, CompactionSource
from modex_agent.memory.core.models import CoreMemoryContents
from modex_agent.memory.pruned.manager import PrunedManager
from modex_agent.memory.scope import MemoryContext
from modex_agent.memory.token_estimator import CharTokenEstimator, TokenEstimator

if TYPE_CHECKING:
    from modex_agent.memory.hooks import MemoryHook


class MemorySystem(ABC):
    """Abstract application-facing memory capability — CRUD lifecycle + injection reads.

    A complete memory system must implement both the lifecycle methods
    (initialize, close, CRUD) and the injection read methods (core memory,
    archive, providers) used by injection policies.
    """

    # -- lifecycle ------------------------------------------------------------

    @abstractmethod
    async def initialize(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    @abstractmethod
    def add_cleanup_hook(self, hook: MemoryHook) -> None: ...

    # -- CRUD -----------------------------------------------------------------

    @abstractmethod
    def create_message_history(
        self,
        context: MemoryContext,
        initial_messages: Sequence[ChatMessage | dict[str, Any]] | None = None,
        *,
        model_info: ModelInfo | None = None,
    ) -> MessageHistory:
        """Create a history for *context*, optionally bound to the turn's model.

        ``model_info`` carries the active model's budget profile
        (context/output limits); history implementations use it to judge
        their append-path compaction trigger against the CURRENT model's
        window instead of the static pool config.
        """
        ...

    @abstractmethod
    async def get_history(
        self,
        context: MemoryContext,
    ) -> list[ChatMessage]: ...

    @abstractmethod
    async def get_full_history(
        self,
        context: MemoryContext,
        *,
        limit: int | None = None,
    ) -> list[ChatMessage]:
        """Return all messages including soft-deleted ones (for context fork).

        COMPACT role messages are excluded at the store layer.  If *limit* is
        provided, returns only the most recent *limit* messages.
        """
        ...

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
    async def get_core_memory(self, context: MemoryContext) -> CoreMemoryContents:
        """Return all long-term core memory for the given context."""
        ...

    @abstractmethod
    async def retrieve_core_memory(
        self,
        context: MemoryContext,
        query: str = "",
    ) -> CoreMemoryContents:
        """Retrieve core memory relevant to a query."""
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
    async def get_core_memory_directory(self, context: MemoryContext) -> Path | None:
        """Return the core memory storage directory."""
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
        *,
        model_info: ModelInfo | None = None,
    ) -> MessageHistory:
        """Create a history for *context*, optionally bound to the turn's model.

        ``model_info`` carries the active model's budget profile
        (context/output limits); history implementations use it to judge
        their append-path compaction trigger against the CURRENT model's
        window instead of the static pool config.
        """
        ...

    @abstractmethod
    async def get_history(
        self,
        context: MemoryContext,
    ) -> list[ChatMessage]: ...

    async def get_full_history(
        self,
        context: MemoryContext,
        *,
        limit: int | None = None,
    ) -> list[ChatMessage]:
        """Default: same as get_history (backends without soft-delete).

        Limit is ignored in this default implementation — backends without
        soft-delete do not support limit on the full-history path.
        """
        _ = limit
        return await self.get_history(context)

    @abstractmethod
    async def clear(self, context: MemoryContext) -> None: ...

    async def compact_session(
        self,
        context: MemoryContext,
        *,
        budget: ContextBudget | None = None,
        source: CompactionSource,
    ) -> CleanupResult:
        """Unified session-compaction entry point. Default no-op.

        Deployments without compaction capability safely return
        ``CleanupResult(triggered=False)``; ``DefaultMemorySystem`` owns the
        real 5-phase pipeline (precedent for a non-abstract no-op hook:
        ``ContextManager.flush``). ``budget`` is the CURRENT TURN's effective
        budget (``resolve_effective_budget`` output — limit plus output
        reservation in one typed value); ``None`` falls back to the
        system-configured budget, semantics identical to the pre-budget
        signature. ``source`` labels the trigger face (write-side append vs
        read-side pre-LLM) on the result and hook payloads.
        """
        _ = context, budget, source
        return CleanupResult(triggered=False)

    @property
    def token_estimator(self) -> TokenEstimator:
        """The estimator this system's compaction trigger counts with.

        Default: the zero-dependency char-based estimator. Concrete systems
        that accept an injected estimator override this so downstream
        trigger faces (e.g. the pre-LLM governance) reuse the SAME
        estimator instead of silently judging pressure with a second one.
        """
        return CharTokenEstimator()
