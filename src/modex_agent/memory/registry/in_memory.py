"""In-memory store registry."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Collection

from modex_agent.memory.archive_models import (
    CONTEXT_ARCHIVE_FILE_KEY,
    KNOWLEDGE_ARCHIVE_FILE_KEY,
    ArchiveChannel,
)
from modex_agent.core.scope import (
    MemoryAgentRole,
    MemoryContext,
    MemoryLayerName,
    MemoryScope,
    ScopeRecord,
    infer_agent_role,
)
from modex_agent.memory.core.storage import MemoryStorage
from modex_agent.memory.registry.base import MemoryStoreRegistry
from modex_agent.memory.stores.scoped_in_memory import InMemoryScopedStorage


class InMemoryStoreRegistry(MemoryStoreRegistry):
    """Registry that creates one in-memory storage object per layer/scope."""

    def __init__(self) -> None:
        self._stores: dict[tuple[MemoryLayerName, str], InMemoryScopedStorage] = {}
        self._records: dict[tuple[MemoryLayerName, str], ScopeRecord] = {}
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        pass

    async def close(self) -> None:
        for storage in self._stores.values():
            await storage.close()

    async def resolve(
        self,
        *,
        layer: MemoryLayerName,
        scope: MemoryScope,
        context: MemoryContext,
    ) -> MemoryStorage:
        scope_key = scope.get_scope_key(context)
        cache_key = (layer, scope_key)
        # Fast path: already cached — no lock needed (dict read is atomic)
        storage = self._stores.get(cache_key)
        if storage is not None:
            self._update_record(cache_key, scope_key, layer, context)
            return storage
        # Slow path: create under lock to prevent TOCTOU race
        async with self._lock:
            # Double-check after acquiring lock
            storage = self._stores.get(cache_key)
            if storage is None:
                storage = InMemoryScopedStorage()
                await storage.initialize()
                self._stores[cache_key] = storage
        self._update_record(cache_key, scope_key, layer, context)
        return storage

    def _update_record(
        self,
        cache_key: tuple[MemoryLayerName, str],
        scope_key: str,
        layer: MemoryLayerName,
        context: MemoryContext,
    ) -> None:
        existing = self._records.get(cache_key)
        self._records[cache_key] = ScopeRecord(
            scope_key=scope_key,
            layer=layer,
            context=context,
            storage_path=f"memory://{layer}/{scope_key}",
            agent_role=infer_agent_role(context),
            agent_id=context.agent_id,
            created_at=existing.created_at if existing is not None else time.time(),
            updated_at=time.time(),
        )

    async def list_records(
        self,
        *,
        layer: MemoryLayerName | None = None,
        agent_roles: Collection[str | MemoryAgentRole] | None = frozenset({MemoryAgentRole.MAIN}),
        has_file: str | None = None,
    ) -> list[ScopeRecord]:
        records = list(self._records.values())
        if layer is not None:
            records = [record for record in records if record.layer == layer]
        if agent_roles is not None:
            allowed_roles = {str(role) for role in agent_roles}
            records = [record for record in records if str(record.agent_role) in allowed_roles]
        if has_file is not None:
            filtered: list[ScopeRecord] = []
            for record in records:
                storage = self._stores.get((MemoryLayerName(record.layer), record.scope_key))
                if storage is None:
                    continue
                logs = await storage.read_logs()
                has_context_archive = any(
                    entry.get("channel") == ArchiveChannel.CONTEXT.value for entry in logs
                )
                has_knowledge_archive = any(
                    entry.get("channel") == ArchiveChannel.KNOWLEDGE.value for entry in logs
                )
                if (
                    has_file == "messages"
                    and await storage.load_messages()
                    or has_file in {"history", "archive", "logs"}
                    and logs
                    or has_file == CONTEXT_ARCHIVE_FILE_KEY
                    and has_context_archive
                    or has_file == KNOWLEDGE_ARCHIVE_FILE_KEY
                    and has_knowledge_archive
                    or has_file == "kv"
                    and await storage.list_keys()
                ):
                    filtered.append(record)
            records = filtered
        return records

    async def evict(
        self,
        *,
        layer: MemoryLayerName | None = None,
        scope: MemoryScope | None = None,
    ) -> None:
        _ = scope
        keys = [key for key in self._stores if layer is None or key[0] == layer]
        for key in keys:
            await self._stores[key].close()
            self._stores.pop(key, None)
