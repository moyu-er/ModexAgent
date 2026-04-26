"""Default session memory manager backed by scoped storage factories."""

from __future__ import annotations

import json
import time
from collections.abc import Mapping, Sequence
from typing import Any

from framework.memory.core.layers import SessionMemoryManager
from framework.memory.core.message import ChatMessage
from framework.memory.core.models import StorageRevision
from framework.memory.core.scope import MemoryContext
from framework.memory.layers.config import SessionMemoryConfig, StorageFactory


class ScopedSessionMemoryManager(SessionMemoryManager):
    """Session layer manager that resolves storage through a StorageFactory."""

    def __init__(
        self,
        storage_factory: StorageFactory,
        config: SessionMemoryConfig | None = None,
    ) -> None:
        self._storage_factory = storage_factory
        self._config = config or SessionMemoryConfig()

    @staticmethod
    def _to_chat_messages(
        messages: Sequence[ChatMessage | dict[str, object]],
    ) -> list[ChatMessage]:
        return [ChatMessage.coerce(message) for message in messages]

    @staticmethod
    def _to_dicts(messages: Sequence[ChatMessage]) -> list[dict[str, object]]:
        return [message.to_dict() for message in messages]

    async def add_messages(
        self,
        context: MemoryContext,
        messages: Sequence[ChatMessage | dict[str, object]],
    ) -> StorageRevision:
        storage = await self._storage_factory(context)
        chat_messages = self._to_chat_messages(messages)

        # write_id-based batch idempotency
        write_ids = {
            m.get("write_id")
            for m in messages
            if hasattr(m, "get") and m.get("write_id")
        }
        if len(write_ids) == 1:
            single_id = next(iter(write_ids))
            if single_id and await storage.get(".last_write_id") == single_id:
                return await storage.get_revision()

        async with storage.get_lock().write():
            existing = await storage.load_messages()
            existing.extend(self._to_dicts(chat_messages))
            await storage.set(".last_activity", time.time())
            if write_ids:
                await storage.set(".last_write_id", next(iter(write_ids)))
            return await storage.save_messages(existing)

    async def get_visible_messages(
        self,
        context: MemoryContext,
        limit: int | None = None,
    ) -> list[ChatMessage]:
        messages = await self.get_all_messages(context)
        effective_limit = limit if limit is not None else self._config.max_messages
        return messages[-effective_limit:] if effective_limit is not None else messages

    async def get_all_messages(self, context: MemoryContext) -> list[ChatMessage]:
        storage = await self._storage_factory(context)
        raw = await storage.load_messages()
        return ChatMessage.from_dicts(raw)

    async def save_checkpoint(
        self,
        context: MemoryContext,
        messages: Sequence[ChatMessage | dict[str, object]],
    ) -> None:
        storage = await self._storage_factory(context)
        chat_messages = self._to_chat_messages(messages)
        await storage.set(
            self._config.checkpoint_key,
            json.dumps({"messages": self._to_dicts(chat_messages)}, ensure_ascii=False),
        )

    async def load_checkpoint(self, context: MemoryContext) -> list[ChatMessage] | None:
        storage = await self._storage_factory(context)
        raw = await storage.get(self._config.checkpoint_key)
        if raw is None:
            return None
        if isinstance(raw, str):
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                return None
        elif isinstance(raw, dict):
            payload = raw
        else:
            return None
        messages = payload.get("messages")
        if not isinstance(messages, list):
            return None
        return ChatMessage.from_dicts(messages)

    async def clear(self, context: MemoryContext) -> None:
        storage = await self._storage_factory(context)
        async with storage.get_lock().write():
            await storage.save_messages([])
            await storage.delete(self._config.checkpoint_key)

    async def replace_messages(
        self,
        context: MemoryContext,
        messages: Sequence[ChatMessage | dict[str, object]],
    ) -> StorageRevision:
        storage = await self._storage_factory(context)
        chat_messages = self._to_chat_messages(messages)
        async with storage.get_lock().write():
            await storage.set(".last_activity", time.time())
            return await storage.save_messages(self._to_dicts(chat_messages))

    async def replace_messages_if_revision(
        self,
        context: MemoryContext,
        messages: Sequence[ChatMessage | dict[str, object]],
        expected_revision: StorageRevision,
        state_updates: Mapping[str, Any] | None = None,
    ) -> StorageRevision | None:
        storage = await self._storage_factory(context)
        chat_messages = self._to_chat_messages(messages)
        async with storage.get_lock().write():
            current = await storage.get_revision()
            if current.version != expected_revision.version:
                return None
            if current.message_count != expected_revision.message_count:
                return None
            for key, value in (state_updates or {}).items():
                await storage.set(key, value)
            await storage.set(".last_activity", time.time())
            return await storage.save_messages(self._to_dicts(chat_messages))

    async def get_revision(self, context: MemoryContext) -> StorageRevision:
        storage = await self._storage_factory(context)
        return await storage.get_revision()

    async def get_state(self, context: MemoryContext, key: str) -> Any | None:
        storage = await self._storage_factory(context)
        return await storage.get(key)

    async def set_state(self, context: MemoryContext, key: str, value: Any) -> None:
        storage = await self._storage_factory(context)
        await storage.set(key, value)
