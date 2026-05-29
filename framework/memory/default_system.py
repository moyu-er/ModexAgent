"""Default tiered memory system with registry-backed layer managers."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

from framework.memory.archive_generation import ArchiveGenerationStrategy
from framework.memory.archive_models import ArchiveChannel
from framework.memory.core.layers import ArchiveMemoryManager, MemoryLayerSet, SessionMemoryManager
from framework.memory.core.message import ChatMessage
from framework.memory.core.models import LongTermMemory
from framework.memory.core.scope import (
    MemoryContext,
    MemoryLayerName,
    SessionScope,
)
from framework.memory.core.system import MemorySystem
from framework.memory.history import MessageHistory
from framework.memory.lifecycle import MemoryMaintenancePolicy
from framework.memory.recorder import MemoryAppendRecorder
from framework.memory.registry.base import MemoryStoreRegistry
from framework.core.types import MessageRole

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
        archive_strategy: ArchiveGenerationStrategy | None = None,
        cleanup_config: dict[str, int | float] | None = None,
        user_retention: Any | None = None,
    ) -> None:
        self._manager = manager
        self._context = context
        self._recorder = recorder
        self._archive_manager = archive_manager
        self._archive_strategy = archive_strategy
        self._cleanup_config: dict[str, int | float] = cleanup_config or {}
        self._user_retention = user_retention
        self._cache: list[ChatMessage] | None = (
            [ChatMessage.coerce(m) for m in initial_messages]
            if initial_messages is not None
            else None
        )
        self._cache_lock = asyncio.Lock()

    async def _run_cleanup(self) -> None:
        from framework.memory.cleanup import cleanup_session

        await cleanup_session(
            session=self._manager,
            archive=self._archive_manager,
            context=self._context,
            archive_strategy=self._archive_strategy,
            user_retention=self._user_retention,
            **self._cleanup_config,
        )

    async def _urb_completion_hook(self, message: ChatMessage | dict[str, Any]) -> None:
        """If message is a plain assistant, mark all URB entries completed."""
        if self._user_retention is None:
            return
        msg_dict = message.to_dict() if hasattr(message, "to_dict") else dict(message)
        if msg_dict.get("role") != str(MessageRole.ASSISTANT):
            return
        if msg_dict.get("tool_calls"):
            return
        assistant_content = msg_dict.get("content")
        if assistant_content is None:
            return
        await self._user_retention.mark_all_completed(self._context, assistant_content)

    async def append(self, message: ChatMessage | dict[str, Any]) -> None:
        await self._manager.add_messages(self._context, [message])
        if self._recorder is not None:
            await self._recorder.record([message], self._context)
        if self._user_retention is not None:
            await self._urb_completion_hook(message)
        await self._run_cleanup()
        async with self._cache_lock:
            self._cache = None

    async def extend(self, messages: Sequence[ChatMessage | dict[str, Any]]) -> None:
        if not messages:
            return
        await self._manager.add_messages(self._context, list(messages))
        if self._recorder is not None:
            await self._recorder.record(list(messages), self._context)
        if self._user_retention is not None and messages:
            await self._urb_completion_hook(messages[-1])
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


class DefaultMemorySystem(MemorySystem):
    """Default tiered memory system that delegates to typed layer managers.

    Receives a ``MemoryLayerSet``, ``MemoryStoreRegistry``, and optional
    ``MemoryProviderRegistry``.  All persistence goes through the
    registry's scoped storage instances.

    This is the single concrete memory system.  There is no legacy
    compatibility path — all callers should migrate to this class.

    This class satisfies the :class:`~framework.memory.core.system.InjectableMemorySystem`
    Protocol via duck typing.  All methods required by the injection policy
    are implemented.
    """

    def __init__(
        self,
        *,
        layer_set: MemoryLayerSet,
        store_registry: MemoryStoreRegistry,
        providers: Any | None = None,  # MemoryProviderRegistry
        archive_strategy: ArchiveGenerationStrategy | None = None,
        cleanup_config: dict[str, int | float] | None = None,
        maintenance_policy: MemoryMaintenancePolicy | None = None,
    ) -> None:
        self._layers = layer_set
        self._registry = store_registry
        self._providers = providers
        self._archive_strategy = archive_strategy
        self._cleanup_config: dict[str, int | float] = cleanup_config or {}
        self._maintenance_policy = maintenance_policy
        self._recorder = MemoryAppendRecorder()
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
            archive_strategy=self._archive_strategy,
            cleanup_config=self._cleanup_config,
            user_retention=self._layers.user_retention,
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
        max_messages: int | None = None,
    ) -> list[ChatMessage]:
        return await self._layers.session.get_recent_messages(context, limit=max_messages)

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
        if self._layers.knowledge is not None:
            await self._layers.knowledge.clear(context)
        if self._layers.user_retention is not None:
            await self._layers.user_retention.clear(context)

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
                "created_at": e.created_at.isoformat() if e.created_at is not None else None,
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

    # -- Knowledge convenience ------------------------------------------

    async def get_knowledge(self, context: MemoryContext) -> LongTermMemory:
        knowledge = self._layers.knowledge
        if knowledge is None:
            return LongTermMemory()
        return await knowledge.get_all(context)

    async def retrieve_knowledge(
        self,
        context: MemoryContext,
        query: str = "",
    ) -> LongTermMemory:
        knowledge = self._layers.knowledge
        if knowledge is None:
            return LongTermMemory()
        return await knowledge.retrieve(context, query=query)

    async def get_knowledge_directory(self, context: MemoryContext) -> Path | None:
        """Return the absolute path to the knowledge storage directory."""
        if self._layers.knowledge is None:
            return None
        try:
            return await self._layers.knowledge.get_storage_path(context)
        except Exception:
            logger.debug("Failed to resolve knowledge directory", exc_info=True)
            return None

    @property
    def knowledge_manager(self) -> Any | None:
        """Expose knowledge manager for DreamEngine compatibility."""
        return self._layers.knowledge

    # -- Provider fan-out -----------------------------------------------

    async def search_memories(
        self, query: str, context: MemoryContext, limit: int = 5
    ) -> list[dict[str, Any]]:
        return await self.search(query, context, limit)

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

    # -- Composition helpers --------------------------------------------

    async def build_system_prompt(
        self,
        context: MemoryContext,
        *,
        max_history_entries: int = 5,
        query: str = "",
    ) -> str:
        """Build a system prompt from knowledge + archive + provider layers."""
        sections: list[str] = []

        # Knowledge (SOUL.md, USER.md, MEMORY.md)
        knowledge = self._layers.knowledge
        if knowledge is not None:
            lt = await knowledge.get_all(context)
            if lt.soul:
                sections.append(f"## 你的沟通风格\n{lt.soul}")
            if lt.user:
                sections.append(f"## 用户画像\n{lt.user}")
            if lt.memory:
                sections.append(f"## 相关知识\n{lt.memory}")
            for key, value in lt.custom.items():
                sections.append(f"## {key}\n{value}")

        # Archive summaries
        if max_history_entries > 0:
            archive = self._layers.archive
            if archive is not None:
                if query:
                    entries = await archive.search(context, query=query, limit=max_history_entries)
                    if not entries:
                        entries = await archive.get_recent(context, limit=max_history_entries)
                else:
                    entries = await archive.get_recent(context, limit=max_history_entries)
                if entries:
                    blocks: list[str] = []
                    for idx, e in enumerate(entries, start=1):
                        if not e.summary:
                            continue
                        time_str = ""
                        if e.created_at is not None:
                            time_str = f" {e.created_at.strftime('%Y-%m-%d %H:%M')}"
                        blocks.append(f"--- [Historical Record {idx}]{time_str} ---\n{e.summary}")
                    if blocks:
                        sections.append("## Historical Context Summaries\n\n" + "\n\n".join(blocks))

        return "\n\n---\n\n".join(sections) if sections else ""

    async def ensure_within_budget(self, context: MemoryContext) -> None:
        """Pre-load budget hook.

        Called by MemorySystemContextManager.load() before every LLM request.
        It must not emit post-write lifecycle events; explicit budget
        enforcement should use a dedicated read/check policy.
        """
        _ = context

    # -- Internal helpers -----------------------------------------------

    async def _resolve_archive_storage(self, context: MemoryContext) -> Any:
        archive = self._layers.archive
        from framework.memory.core.scope import UserScope
        scope = archive.get_scope() if archive is not None else UserScope()
        return await self._registry.resolve(
            layer=MemoryLayerName.ARCHIVE,
            scope=scope,
            context=context,
        )
