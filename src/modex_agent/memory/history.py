"""Concrete in-memory and scoped message histories."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator, Sequence
from typing import Any

from modex_agent.core import MessageHistory as _MessageHistory
from modex_agent.core.capabilities import ModelInfo
from modex_agent.core.message import ChatMessage
from modex_agent.memory.budget import ContextBudget, resolve_effective_budget
from modex_agent.memory.core.layers import SessionMemoryManager
from modex_agent.memory.core.system import ContextManagedMemorySystem
from modex_agent.memory.recorder import MemoryAppendRecorder
from modex_agent.memory.scope import MemoryContext
from modex_agent.memory.token_estimator import CharTokenEstimator, TokenEstimator


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
        memory_system: ContextManagedMemorySystem,
        initial_messages: Sequence[ChatMessage | dict[str, Any]] | None = None,
        recorder: MemoryAppendRecorder | None = None,
        cleanup_config: dict[str, int | float] | None = None,
        token_estimator: TokenEstimator | None = None,
        model_info: ModelInfo | None = None,
    ) -> None:
        self._manager = manager
        self._context = context
        self._memory_system = memory_system
        self._recorder = recorder
        self._cleanup_config: dict[str, int | float] = cleanup_config or {}
        self._token_estimator: TokenEstimator = token_estimator or CharTokenEstimator()
        self._model_info: ModelInfo | None = model_info
        self._cache: list[ChatMessage] | None = (
            [ChatMessage.coerce(message) for message in initial_messages]
            if initial_messages is not None
            else None
        )
        self._cache_lock = asyncio.Lock()

    def _effective_budget(self) -> ContextBudget:
        """Resolve the budget this history's trigger judges against.

        Per-turn ``model_info`` (bound at ``load()`` from the active model)
        wins over the static pool ``cleanup_config``; the priority chain is
        defined ONLY in ``memory/budget.py`` — never expanded here.
        """
        max_context_tokens = self._cleanup_config.get("max_context_tokens")
        return resolve_effective_budget(
            self._model_info,
            int(max_context_tokens) if max_context_tokens is not None else None,
            int(self._cleanup_config.get("max_output_tokens", 0)),
        )

    async def _run_cleanup_if_triggered(self) -> bool:
        if not self._is_trigger_condition_met():
            return False
        from modex_agent.memory.cleanup import CompactionSource

        budget = self._effective_budget()
        result = await self._memory_system.compact_session(
            self._context,
            budget=budget,
            source=CompactionSource.POST_APPEND,
        )
        return result.triggered

    def _is_trigger_condition_met(self) -> bool:
        if self._cache is None or not self._cache:
            return True
        from modex_agent.memory.cleanup import check_cleanup_trigger

        budget = self._effective_budget()
        reason = check_cleanup_trigger(
            self._cache,
            self._token_estimator,
            budget.max_context_tokens,
            float(self._cleanup_config.get("max_token_ratio", 0.85)),
            budget.max_output_tokens,
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

    async def refresh(self) -> None:
        """Drop the read cache — the next ``to_list`` re-reads the store.

        Called after an external writer (pre-LLM compaction governance)
        compacted the session through ``memory_system.compact_session``,
        bypassing this history's own append path.
        """
        await self._invalidate_cache()

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
            await self._invalidate_cache()
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
            await self._invalidate_cache()
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
