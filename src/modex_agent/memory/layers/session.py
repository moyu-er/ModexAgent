"""Default session memory manager backed by scoped storage factories."""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from typing import Any

from modex_agent.core.message import ChatMessage
from modex_agent.memory.core.layers import SessionMemoryManager
from modex_agent.memory.core.lock import StorageLock
from modex_agent.memory.core.models import StorageRevision
from modex_agent.memory.core.split_stores import MemoryStoreBundle
from modex_agent.memory.core.store_metadata import StoreMetadata
from modex_agent.memory.layers.config import SessionMemoryConfig, StorageFactory
from modex_agent.memory.scope import MemoryContext


def _get_bundle_lock(bundle: MemoryStoreBundle) -> StorageLock | None:
    """Return the storage lock from the bundle's concrete store, or None."""
    store = bundle.messages
    if isinstance(store, StoreMetadata):
        return store.get_lock()
    return None


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
        bundle = await self._storage_factory(context)
        chat_messages = self._to_chat_messages(messages)

        # write_id-based batch idempotency
        write_ids = {m.get("write_id") for m in messages if hasattr(m, "get") and m.get("write_id")}
        if len(write_ids) == 1:
            single_id = next(iter(write_ids))
            if single_id and await bundle.kv.get(".last_write_id") == single_id:
                return await bundle.messages.get_revision()

        lock = _get_bundle_lock(bundle)
        if lock is not None:
            async with lock.write():
                return await self._add_messages_locked(bundle, chat_messages, messages, write_ids)
        return await self._add_messages_locked(bundle, chat_messages, messages, write_ids)

    async def _add_messages_locked(
        self,
        bundle: MemoryStoreBundle,
        chat_messages: list[ChatMessage],
        messages: Sequence[ChatMessage | dict[str, object]],
        write_ids: set[Any],
    ) -> StorageRevision:
        dicts = self._to_dicts(chat_messages)
        revision = await bundle.messages.get_revision()
        for d in dicts:
            revision = await bundle.messages.append_message(d)
        await bundle.kv.set(".last_activity", time.time())
        if write_ids:
            await bundle.kv.set(".last_write_id", next(iter(write_ids)))
        return revision

    async def get_recent_messages(
        self,
        context: MemoryContext,
        limit: int | None = None,
    ) -> list[ChatMessage]:
        messages = await self.get_all_messages(context)
        return messages[-limit:] if limit is not None else messages

    async def get_all_messages(self, context: MemoryContext) -> list[ChatMessage]:
        bundle = await self._storage_factory(context)
        raw = await bundle.messages.load_messages()
        return ChatMessage.from_dicts(raw)

    async def get_all_messages_raw(
        self,
        context: MemoryContext,
        *,
        limit: int | None = None,
    ) -> list[ChatMessage]:
        bundle = await self._storage_factory(context)
        raw = await bundle.messages.load_all_messages(limit=limit)
        return ChatMessage.from_dicts(raw)

    async def retain_messages(
        self,
        context: MemoryContext,
        keep_messages: Sequence[ChatMessage | dict[str, object]],
        expected_revision: StorageRevision,
    ) -> StorageRevision | None:
        bundle = await self._storage_factory(context)
        chat_messages = self._to_chat_messages(keep_messages)
        keep_dicts = self._to_dicts(chat_messages)
        lock = _get_bundle_lock(bundle)

        async def _do_retain() -> StorageRevision | None:
            return await bundle.messages.retain_messages(keep_dicts, expected_revision)

        if lock is not None:
            async with lock.write():
                return await _do_retain()
        return await _do_retain()

    async def clear(self, context: MemoryContext) -> None:
        bundle = await self._storage_factory(context)
        lock = _get_bundle_lock(bundle)
        if lock is not None:
            async with lock.write():
                await bundle.messages.save_messages([])
        else:
            await bundle.messages.save_messages([])

    async def replace_messages(
        self,
        context: MemoryContext,
        messages: Sequence[ChatMessage | dict[str, object]],
    ) -> StorageRevision:
        bundle = await self._storage_factory(context)
        chat_messages = self._to_chat_messages(messages)
        lock = _get_bundle_lock(bundle)
        if lock is not None:
            async with lock.write():
                await bundle.kv.set(".last_activity", time.time())
                result = await bundle.messages.replace_active_messages(
                    self._to_dicts(chat_messages)
                )
                return result or await bundle.messages.get_revision()
        await bundle.kv.set(".last_activity", time.time())
        result = await bundle.messages.replace_active_messages(
            self._to_dicts(chat_messages)
        )
        return result or await bundle.messages.get_revision()

    async def replace_messages_if_revision(
        self,
        context: MemoryContext,
        messages: Sequence[ChatMessage | dict[str, object]],
        expected_revision: StorageRevision,
        state_updates: Mapping[str, Any] | None = None,
        idle_threshold_seconds: float | None = None,
    ) -> StorageRevision | None:
        bundle = await self._storage_factory(context)
        chat_messages = self._to_chat_messages(messages)
        lock = _get_bundle_lock(bundle)

        async def _do_replace() -> StorageRevision | None:
            if idle_threshold_seconds is not None:
                last = await bundle.kv.get(".last_activity")
                if isinstance(last, int | float) and time.time() - last <= idle_threshold_seconds:
                    return None
            current = await bundle.messages.get_revision()
            if current.version != expected_revision.version:
                return None
            if current.message_count != expected_revision.message_count:
                return None
            for key, value in (state_updates or {}).items():
                await bundle.kv.set(key, value)
            await bundle.kv.set(".last_activity", time.time())
            return await bundle.messages.replace_active_messages(
                self._to_dicts(chat_messages), expected_revision
            )

        if lock is not None:
            async with lock.write():
                return await _do_replace()
        return await _do_replace()

    async def get_revision(self, context: MemoryContext) -> StorageRevision:
        bundle = await self._storage_factory(context)
        return await bundle.messages.get_revision()

    async def get_state(self, context: MemoryContext, key: str) -> Any | None:
        bundle = await self._storage_factory(context)
        return await bundle.kv.get(key)

    async def set_state(self, context: MemoryContext, key: str, value: Any) -> None:
        bundle = await self._storage_factory(context)
        await bundle.kv.set(key, value)
