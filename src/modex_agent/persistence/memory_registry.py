"""Hybrid memory registry for SQLite-backed structured state and file documents."""

from __future__ import annotations

from collections.abc import Collection
from pathlib import Path

from modex_agent.core.scope import (
    MemoryAgentRole,
    MemoryContext,
    MemoryLayerName,
    RecordScope,
    Scope,
    ScopeRecord,
)
from modex_agent.memory.core.split_stores import MemoryStoreBundle
from modex_agent.memory.registry import DefaultMemoryStoreRegistry, MemoryStoreRegistry
from modex_agent.persistence.managers.workspace import WorkspacePersistenceManager


class HybridMemoryStoreRegistry(MemoryStoreRegistry):
    """Route structured memory state to SQLite while retaining file documents."""

    def __init__(
        self,
        *,
        file_root: Path,
        persistence: WorkspacePersistenceManager,
        base_scope: RecordScope,
    ) -> None:
        self._files = DefaultMemoryStoreRegistry(file_root)
        self._persistence = persistence
        self._base_scope = base_scope

    async def initialize(self) -> None:
        await self._files.initialize()

    async def close(self) -> None:
        await self._files.close()

    async def resolve(
        self,
        *,
        layer: MemoryLayerName,
        scope: Scope,
        context: MemoryContext,
    ) -> MemoryStoreBundle:
        record_scope = self._base_scope.merge(scope.extract(context))
        match layer:
            case MemoryLayerName.CORE | MemoryLayerName.PROVIDER:
                return await self._files.resolve(
                    layer=layer,
                    scope=scope,
                    context=context,
                )
            case MemoryLayerName.ARCHIVE:
                files = await self._files.resolve(
                    layer=layer,
                    scope=scope,
                    context=context,
                )
                structured = self._persistence.create_bundle(record_scope)
                return MemoryStoreBundle(
                    messages=files.messages,
                    kv=structured.kv,
                    cursors=structured.cursors,
                    archive=structured.archive,
                )
            case MemoryLayerName.SESSION | MemoryLayerName.USER_RETENTION:
                return self._persistence.create_bundle(
                    record_scope,
                    with_archive=False,
                )

    async def list_records(
        self,
        *,
        layer: MemoryLayerName | None = None,
        agent_roles: Collection[str | MemoryAgentRole] | None = frozenset({MemoryAgentRole.MAIN}),
        has_file: str | None = None,
    ) -> list[ScopeRecord]:
        return await self._files.list_records(
            layer=layer,
            agent_roles=agent_roles,
            has_file=has_file,
        )

    async def evict(
        self,
        *,
        layer: MemoryLayerName | None = None,
        scope: Scope | None = None,
    ) -> None:
        await self._files.evict(layer=layer, scope=scope)


__all__ = ["HybridMemoryStoreRegistry"]
