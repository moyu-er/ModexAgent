"""Concrete in-memory and scoped message histories."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator, Sequence
from typing import TYPE_CHECKING, Any

from modex_agent.core import MessageHistory as _MessageHistory
from modex_agent.core.message import ChatMessage
from modex_agent.memory.core.layers import ArchiveMemoryManager, SessionMemoryManager
from modex_agent.memory.hooks import MemoryHookRunner
from modex_agent.memory.pruned.manager import PrunedManager
from modex_agent.memory.recorder import MemoryAppendRecorder
from modex_agent.memory.scope import MemoryContext
from modex_agent.memory.token_estimator import CharTokenEstimator, TokenEstimator

if TYPE_CHECKING:
    from modex_agent.agents.summarizer.abc import ArchiveGenerator
    from modex_agent.memory.stores.dir_archive import DirArchiveStorage


class ListMessageHistory(_MessageHistory):
    """Simple in-memory MessageHistory backed by a Python list."""

    def __init__(self, messages: Sequence[ChatMessage | dict[str, Any]] | None = None) -> None:
        self._messages: list[ChatMessage] = []
        if messages:
            for message in messages:
                self._messages.append(self._coerce(message))

    @staticmethod
    def _coerce(message: ChatMessage | dict[str, Any]) -> ChatMessage:
        return ChatMessage.coerce(message)

    async def append(self, message: ChatMessage | dict[str, Any]) -> None:
        self._messages.append(self._coerce(message))

    async def extend(self, messages: Sequence[ChatMessage | dict[str, Any]]) -> None:
        for message in messages:
            self._messages.append(self._coerce(message))

    async def to_list(self) -> list[ChatMessage]:
        return list(self._messages)

    async def replace_all(
        self, messages: Sequence[ChatMessage | dict[str, Any]], *, skip_transform: bool = False
    ) -> None:
        _ = skip_transform
        self._messages = []
        for message in messages:
            self._messages.append(self._coerce(message))

    def __len__(self) -> int:
        return len(self._messages)

    def __iter__(self) -> Iterator[ChatMessage]:
        return iter(self._messages)

    def __getitem__(self, index: int) -> ChatMessage:
        return self._messages[index]

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(messages={len(self._messages)})"


class ScopedMessageHistory(_MessageHistory):
    """Registry-backed history with a write-through read cache."""

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
            [ChatMessage.coerce(message) for message in initial_messages]
            if initial_messages is not None
            else None
        )
        self._cache_lock = asyncio.Lock()

    async def _run_cleanup_if_triggered(self) -> bool:
        if not self._is_trigger_condition_met():
            return False
        from modex_agent.memory.cleanup import cleanup_session

        max_context_tokens = self._cleanup_config.get("max_context_tokens")
        result = await cleanup_session(
            session=self._manager,
            archive=self._archive_manager,
            context=self._context,
            compactor=self._compactor,
            max_context_tokens=(
                int(max_context_tokens) if max_context_tokens is not None else None
            ),
            max_token_ratio=float(self._cleanup_config.get("max_token_ratio", 0.85)),
            max_output_tokens=int(self._cleanup_config.get("max_output_tokens", 0)),
            keep_ratio=float(self._cleanup_config.get("keep_ratio", 0.3)),
            max_backups=int(self._cleanup_config.get("max_backups", 10)),
            pruned_manager=self._pruned_manager,
            archive_agent=self._archive_agent,
            archive_storage=self._archive_storage,
            hook_runner=self._hook_runner,
            token_estimator=self._token_estimator,
        )
        return result.triggered

    def _is_trigger_condition_met(self) -> bool:
        if self._cache is None or not self._cache:
            return True
        from modex_agent.memory.cleanup import check_cleanup_trigger

        max_context_tokens = self._cleanup_config.get("max_context_tokens")
        reason = check_cleanup_trigger(
            self._cache,
            self._token_estimator,
            int(max_context_tokens) if max_context_tokens is not None else None,
            float(self._cleanup_config.get("max_token_ratio", 0.85)),
            int(self._cleanup_config.get("max_output_tokens", 0)),
        )
        return reason is not None

    async def _refresh_cache(self) -> list[ChatMessage]:
        async with self._cache_lock:
            if self._cache is not None:
                return list(self._cache)
        recent = await self._manager.get_recent_messages(self._context)
        async with self._cache_lock:
            self._cache = list(recent)
        return list(recent)

    def _append_to_cache(self, messages: Sequence[ChatMessage | dict[str, Any]]) -> None:
        if self._cache is None:
            return
        for message in messages:
            self._cache.append(ChatMessage.coerce(message))

    async def _invalidate_cache(self) -> None:
        async with self._cache_lock:
            self._cache = None

    def _stamp_token_count(
        self, messages: Sequence[ChatMessage | dict[str, Any]]
    ) -> list[ChatMessage | dict[str, Any]]:
        stamped: list[ChatMessage | dict[str, Any]] = []
        for message in messages:
            chat = ChatMessage.coerce(message)
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
        compacted = await self._run_cleanup_if_triggered()
        if compacted:
            await self._refresh_cache()
        else:
            self._append_to_cache([stamped])

    async def extend(self, messages: Sequence[ChatMessage | dict[str, Any]]) -> None:
        if not messages:
            return
        stamped = self._stamp_token_count(messages)
        await self._manager.add_messages(self._context, stamped)
        if self._recorder is not None:
            await self._recorder.record(stamped, self._context)
        compacted = await self._run_cleanup_if_triggered()
        if compacted:
            await self._refresh_cache()
        else:
            self._append_to_cache(stamped)

    async def to_list(self) -> list[ChatMessage]:
        async with self._cache_lock:
            if self._cache is not None:
                return list(self._cache)
        return await self._refresh_cache()

    async def clear(self) -> None:
        await self._manager.clear(self._context)
        await self._invalidate_cache()

    async def replace_all(
        self, messages: Sequence[ChatMessage | dict[str, Any]], *, skip_transform: bool = False
    ) -> None:
        _ = skip_transform
        await self._manager.replace_messages(self._context, list(messages))
        await self._invalidate_cache()

    def __len__(self) -> int:
        raise RuntimeError("Use 'await history.to_list()' for async access.")

    def __iter__(self) -> Iterator[ChatMessage]:
        raise RuntimeError("Use 'await history.to_list()' for async access.")

    def __getitem__(self, index: int) -> ChatMessage:
        raise RuntimeError("Use 'await history.to_list()' for async access.")


__all__ = ["ListMessageHistory", "ScopedMessageHistory"]
