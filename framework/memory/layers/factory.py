"""Factory for default registry-backed memory layer sets."""

from __future__ import annotations

from framework.memory.core.layers import MemoryLayerSet
from framework.memory.core.scope import (
    MemoryContext,
    MemoryLayerName,
    MemoryScope,
    SessionScope,
    UserScope,
)
from framework.memory.core.storage import MemoryStorage
from framework.memory.layers.archive import ScopedArchiveMemoryManager
from framework.memory.layers.config import MemoryLayerConfigSet, SessionMemoryConfig, StorageFactory
from framework.memory.layers.knowledge import ScopedKnowledgeMemoryManager
from framework.memory.layers.session import ScopedSessionMemoryManager
from framework.memory.registry import MemoryStoreRegistry


class MemoryLayerFactory:
    """Build typed layer sets using a MemoryStoreRegistry."""

    @staticmethod
    def single_user(
        *,
        registry: MemoryStoreRegistry,
        config: MemoryLayerConfigSet | None = None,
        llm_provider: object | None = None,
    ) -> MemoryLayerSet:
        _ = llm_provider
        config = config or MemoryLayerConfigSet()
        session_manager = ScopedSessionMemoryManager(
            MemoryLayerFactory._storage_factory(
                registry, MemoryLayerName.SESSION, config.session.scope
            ),
            config.session,
        )
        archive_manager = (
            ScopedArchiveMemoryManager(
                MemoryLayerFactory._storage_factory(
                    registry, MemoryLayerName.ARCHIVE, config.archive.scope
                ),
                config.archive,
            )
            if config.archive is not None
            else None
        )
        knowledge_manager = (
            ScopedKnowledgeMemoryManager(
                MemoryLayerFactory._storage_factory(
                    registry, MemoryLayerName.KNOWLEDGE, config.knowledge.scope
                ),
                config.knowledge,
            )
            if config.knowledge is not None
            else None
        )
        return MemoryLayerSet(
            session=session_manager,
            archive=archive_manager,
            knowledge=knowledge_manager,
        )

    @staticmethod
    def session_only(
        *,
        registry: MemoryStoreRegistry,
        config: SessionMemoryConfig | None = None,
    ) -> MemoryLayerSet:
        session_manager = ScopedSessionMemoryManager(
            MemoryLayerFactory._storage_factory(
                registry,
                MemoryLayerName.SESSION,
                (config or SessionMemoryConfig()).scope,
            ),
            config,
        )
        return MemoryLayerSet(session=session_manager)

    @staticmethod
    def _storage_factory(
        registry: MemoryStoreRegistry,
        layer: MemoryLayerName,
        scope: MemoryScope | None = None,
    ) -> StorageFactory:
        effective_scope = scope or (SessionScope() if layer == MemoryLayerName.SESSION else UserScope())

        async def resolve(context: MemoryContext) -> MemoryStorage:
            return await registry.resolve(layer=layer, scope=effective_scope, context=context)

        return resolve
