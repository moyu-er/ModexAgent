"""Default tiered memory system with registry-backed layer managers."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from modex_agent.core.message import ChatMessage
from modex_agent.core.scope import (
    MemoryContext,
    MemoryLayerName,
)
from modex_agent.memory.archive_models import ArchiveChannel
from modex_agent.memory.core.layers import (
    ArchiveMemoryManager,
    MemoryLayerSet,
    SessionMemoryManager,
)
from modex_agent.memory.core.models import CoreMemoryContents
from modex_agent.memory.core.system import (
    ContextManagedMemorySystem,
    MemorySystem,
)
from modex_agent.memory.history import MessageHistory
from modex_agent.memory.hooks import MemoryHook, MemoryHookRunner
from modex_agent.memory.token_estimator import CharTokenEstimator, TokenEstimator

if TYPE_CHECKING:
    from modex_agent.agents.summarizer.abc import ArchiveGenerator, CoreMemoryConsolidatorBase
    from modex_agent.memory.stores.dir_archive import DirArchiveStorage
from modex_agent.memory.pruned.manager import PrunedManager
from modex_agent.memory.recorder import MemoryAppendRecorder
from modex_agent.memory.registry.base import MemoryStoreRegistry

logger = logging.getLogger(__name__)


class ScopedMessageHistory(MessageHistory):
    """MessageHistory backed by a registry-scoped SessionMemoryManager.

    Runs ``cleanup_session()`` after every ``append`` / ``extend`` so that
    session pruning and optional archival happen on the ReAct-turn hot path.
    """

    def __init__(
        self,
        manager: SessionMemoryManager,
        context: MemoryContext,
        initial_messages: Sequence[ChatMessage | dict[str, Any]] | None = None,
        recorder: MemoryAppendRecorder | None = None,
        archive_manager: ArchiveMemoryManager | None = None,
        cleanup_config: dict[str, int | float] | None = None,
        pruned_manager: PrunedManager | None = None,
        archive_agent: ArchiveGenerator | None = None,
        archive_storage: DirArchiveStorage | None = None,
        hook_runner: MemoryHookRunner | None = None,
        token_estimator: TokenEstimator | None = None,
        compactor: Any | None = None,
    ) -> None:
        self._manager = manager
        self._context = context
        self._recorder = recorder
        self._archive_manager = archive_manager
        self._cleanup_config: dict[str, int | float] = cleanup_config or {}
        self._pruned_manager: PrunedManager | None = pruned_manager
        self._archive_agent = archive_agent
        self._archive_storage = archive_storage
        self._hook_runner: MemoryHookRunner | None = hook_runner
        self._token_estimator: TokenEstimator = token_estimator or CharTokenEstimator()
        self._compactor = compactor
        self._cache: list[ChatMessage] | None = (
            [ChatMessage.coerce(m) for m in initial_messages]
            if initial_messages is not None
            else None
        )
        self._cache_lock = asyncio.Lock()

    async def _run_cleanup(self) -> None:
        from modex_agent.memory.cleanup import cleanup_session

        await cleanup_session(
            session=self._manager,
            archive=self._archive_manager,
            context=self._context,
            compactor=self._compactor,
            pruned_manager=self._pruned_manager,
            archive_agent=self._archive_agent,
            archive_storage=self._archive_storage,
            hook_runner=self._hook_runner,
            token_estimator=self._token_estimator,
            **self._cleanup_config,
        )

    def _stamp_token_count(
        self, messages: Sequence[ChatMessage | dict[str, Any]]
    ) -> list[ChatMessage | dict[str, Any]]:
        """Set token_count on each message via the estimator (append-time write point)."""
        stamped: list[ChatMessage | dict[str, Any]] = []
        for msg in messages:
            chat = ChatMessage.coerce(msg)
            if chat.token_count is None:
                chat = chat.model_copy(
                    update={"token_count": self._token_estimator.estimate_message(chat)}
                )
            stamped.append(chat)
        return stamped

    async def append(self, message: ChatMessage | dict[str, Any]) -> None:
        [stamped] = self._stamp_token_count([message])
        await self._manager.add_messages(self._context, [stamped])
        if self._recorder is not None:
            await self._recorder.record([stamped], self._context)
        await self._run_cleanup()
        async with self._cache_lock:
            self._cache = None

    async def extend(self, messages: Sequence[ChatMessage | dict[str, Any]]) -> None:
        if not messages:
            return
        stamped = self._stamp_token_count(messages)
        await self._manager.add_messages(self._context, stamped)
        if self._recorder is not None:
            await self._recorder.record(stamped, self._context)
        await self._run_cleanup()
        async with self._cache_lock:
            self._cache = None

    async def to_list(self) -> list[ChatMessage]:
        async with self._cache_lock:
            if self._cache is not None:
                return list(self._cache)
        recent = await self._manager.get_recent_messages(self._context)
        async with self._cache_lock:
            self._cache = list(recent)
        return list(recent)

    async def clear(self) -> None:
        await self._manager.clear(self._context)
        async with self._cache_lock:
            self._cache = None

    async def replace_all(
        self, messages: Sequence[ChatMessage | dict[str, Any]], *, skip_transform: bool = False
    ) -> None:
        _ = skip_transform
        await self._manager.replace_messages(self._context, list(messages))
        async with self._cache_lock:
            self._cache = None

    def __len__(self) -> int:
        raise RuntimeError("Use 'await history.to_list()' for async access.")

    def __iter__(self) -> Iterator[ChatMessage]:
        raise RuntimeError("Use 'await history.to_list()' for async access.")

    def __getitem__(self, index: int) -> ChatMessage:
        raise RuntimeError("Use 'await history.to_list()' for async access.")


class DefaultMemorySystem(MemorySystem, ContextManagedMemorySystem):
    """Default tiered memory system that delegates to typed layer managers.

    Receives a ``MemoryLayerSet``, ``MemoryStoreRegistry``, and optional
    ``MemoryProviderRegistry``.  All persistence goes through the
    registry's scoped storage instances.

    This is the single concrete memory system.  There is no legacy
    compatibility path — all callers should migrate to this class.
    """

    def __init__(
        self,
        *,
        layer_set: MemoryLayerSet,
        store_registry: MemoryStoreRegistry,
        providers: Any | None = None,
        cleanup_config: dict[str, int | float] | None = None,
        pruned_manager: PrunedManager | None = None,
        archive_agent: ArchiveGenerator | None = None,
        archive_storage: DirArchiveStorage | None = None,
        core_memory_consolidator: CoreMemoryConsolidatorBase | None = None,
        token_estimator: TokenEstimator | None = None,
        compactor: Any | None = None,
    ) -> None:
        self._layers = layer_set
        self._registry = store_registry
        self._providers = providers
        self._cleanup_config: dict[str, int | float] = cleanup_config or {}
        self._pruned_manager: PrunedManager | None = pruned_manager
        self._archive_agent = archive_agent
        self._archive_storage = archive_storage
        self._core_memory_consolidator = core_memory_consolidator
        self._token_estimator: TokenEstimator = token_estimator or CharTokenEstimator()
        self._hook_runner = MemoryHookRunner()
        self._recorder = MemoryAppendRecorder()
        self._compactor = compactor
        if providers is not None:
            for provider in providers.all():
                self._recorder.add_provider(provider)

    # -- MemorySystem ABC ------------------------------------------------

    async def initialize(self) -> None:
        await self._registry.initialize()

    async def close(self) -> None:
        await self._registry.close()
        await self._recorder.flush()
        if self._providers is not None:
            await self._providers.shutdown_all()

    def add_cleanup_hook(self, hook: MemoryHook) -> None:
        """Register a memory lifecycle hook for cleanup dispatch.

        Hooks are forwarded to every ``ScopedMessageHistory`` via the shared
        ``MemoryHookRunner`` (passed by reference at creation time). Late
        registration works: a hook added after a history is created still
        receives subsequent events because the runner is the same object.
        """
        self._hook_runner.add(hook)

    def create_message_history(
        self,
        context: MemoryContext,
        initial_messages: Sequence[ChatMessage | dict[str, Any]] | None = None,
    ) -> MessageHistory:
        return ScopedMessageHistory(
            manager=self._layers.session,
            context=context,
            initial_messages=initial_messages,
            recorder=self._recorder,
            archive_manager=self._layers.archive,
            cleanup_config=self._cleanup_config,
            pruned_manager=self._pruned_manager,
            archive_agent=self._archive_agent,
            archive_storage=self._archive_storage,
            hook_runner=self._hook_runner,
            token_estimator=self._token_estimator,
            compactor=self._compactor,
        )

    async def add_messages(
        self,
        context: MemoryContext,
        messages: Sequence[ChatMessage | dict[str, Any]],
    ) -> None:
        if not messages:
            return
        await self._layers.session.add_messages(context, messages)
        await self._recorder.record(list(messages), context)
        # No lifecycle callback — cleanup happens in ScopedMessageHistory

    async def get_history(
        self,
        context: MemoryContext,
    ) -> list[ChatMessage]:
        return await self._layers.session.get_recent_messages(context)

    async def get_full_history(
        self,
        context: MemoryContext,
    ) -> list[ChatMessage]:
        return await self._layers.session.get_all_messages_raw(context)

    async def search(
        self,
        query: str,
        context: MemoryContext,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        archive = self._layers.archive
        if archive is not None:
            try:
                archive_results = await archive.search(context, query=query, limit=limit)
                results.extend(
                    {
                        "summary": entry.summary,
                        "metadata": dict(entry.metadata),
                        "cursor": entry.entry_id,
                        "source": "archive",
                    }
                    for entry in archive_results
                )
            except Exception:
                logger.debug("Archive search failed", exc_info=True)
        for provider in self._recorder.providers:
            try:
                provider_results = await provider.search(query, context, limit)
                results.extend(provider_results)
            except Exception:
                logger.debug("Provider search failed", exc_info=True)
        return results[:limit]

    async def clear(self, context: MemoryContext) -> None:
        await self._layers.session.clear(context)
        if self._layers.archive is not None:
            await self._layers.archive.clear(context)
        if self._layers.core is not None:
            await self._layers.core.clear(context)

    # -- Provider management ---------------------------------------------

    def add_provider(self, provider: Any) -> None:
        self._recorder.add_provider(provider)

    def get_providers(self) -> list[Any]:
        return list(self._recorder.providers)

    # -- Layer access ---------------------------------------------------

    @property
    def layers(self) -> MemoryLayerSet:
        return self._layers

    @property
    def store_registry(self) -> MemoryStoreRegistry:
        return self._registry

    @property
    def pruned_manager(self) -> PrunedManager | None:
        return self._pruned_manager

    # -- Archive convenience --------------------------------------------

    async def get_history_entries(
        self,
        context: MemoryContext,
        limit: int = 5,
        query: str = "",
        *,
        channel: ArchiveChannel = ArchiveChannel.CONTEXT,
    ) -> list[dict[str, Any]]:
        archive = self._layers.archive
        if archive is None:
            return []
        if query:
            entries = await archive.search(context, query=query, limit=limit, channel=channel)
            if not entries:
                entries = await archive.get_recent(context, limit=limit, channel=channel)
        else:
            entries = await archive.get_recent(context, limit=limit, channel=channel)
        return [
            {
                "summary": e.summary,
                "metadata": dict(e.metadata),
                "archive_id": e.entry_id,
                "cursor": e.entry_id,
                "created_at": int(e.created_at.timestamp() * 1000)
                if e.created_at is not None
                else None,
            }
            for e in entries
        ]

    async def get_unprocessed_history_count(
        self, context: MemoryContext, cursor_name: str = "dream"
    ) -> int:
        archive = self._layers.archive
        if archive is None:
            return 0
        result = await archive.get_unprocessed(context, cursor_name, limit=0)
        return len(result.entries)

    @property
    def archive_manager(self) -> Any | None:
        """Expose archive manager for DreamEngine compatibility."""
        return self._layers.archive

    # -- Core memory convenience ------------------------------------------

    async def get_core_memory(self, context: MemoryContext) -> CoreMemoryContents:
        """Return all long-term core memory for the given context."""
        core_memory = self._layers.core
        if core_memory is None:
            return CoreMemoryContents()
        return await core_memory.get_all(context)

    async def retrieve_core_memory(
        self,
        context: MemoryContext,
        query: str = "",
    ) -> CoreMemoryContents:
        """Retrieve core memory relevant to a query."""
        core_memory = self._layers.core
        if core_memory is None:
            return CoreMemoryContents()
        return await core_memory.retrieve(context, query=query)

    async def get_core_memory_directory(self, context: MemoryContext) -> Path | None:
        """Return the absolute path to the core memory storage directory."""
        if self._layers.core is None:
            return None
        try:
            return await self._layers.core.get_storage_path(context)
        except Exception:
            logger.debug("Failed to resolve core memory directory", exc_info=True)
            return None

    async def get_storage_path(self, context: MemoryContext) -> Path | None:
        """Return the absolute path to the archive storage directory."""
        if self._layers.archive is None:
            return None
        try:
            return await self._layers.archive.get_storage_path(context)
        except Exception:
            logger.debug("Failed to resolve archive directory", exc_info=True)
            return None

    @property
    def core_memory_manager(self) -> Any | None:
        """Expose core memory manager for DreamEngine compatibility."""
        return self._layers.core

    @property
    def core_memory_consolidator(self) -> CoreMemoryConsolidatorBase | None:
        """Expose core memory consolidator for DreamEngine wiring."""
        return self._core_memory_consolidator

    # -- Provider fan-out -----------------------------------------------

    async def prefetch_memories(self, query: str, context: MemoryContext) -> str | None:
        if not self._recorder.providers:
            return None
        blocks: list[str] = []
        for provider in self._recorder.providers:
            try:
                block = await provider.prefetch(query, context)
                if block:
                    blocks.append(block)
            except Exception:
                logger.debug("Provider prefetch failed", exc_info=True)
        return "\n\n".join(blocks) if blocks else None

    async def ensure_within_budget(self, context: MemoryContext) -> None:
        """Pre-load budget hook.

        Called by MemorySystemContextManager.load() before every LLM request.
        It must not emit post-write lifecycle events; explicit budget
        enforcement should use a dedicated read/check policy.
        """
        _ = context

    async def _resolve_archive_storage(self, context: MemoryContext) -> Any:
        archive = self._layers.archive
        from modex_agent.core.scope import UserScope

        scope = archive.get_scope() if archive is not None else UserScope()
        return await self._registry.resolve(
            layer=MemoryLayerName.ARCHIVE,
            scope=scope,
            context=context,
        )
