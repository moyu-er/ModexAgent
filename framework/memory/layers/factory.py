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
from framework.memory.layers.config import (
    MemoryLayerConfigSet,
    PendingPrunedInputMemoryConfig,
    SessionMemoryConfig,
    StorageFactory,
)
from framework.memory.layers.knowledge import ScopedKnowledgeMemoryManager
from framework.memory.layers.pending import ScopedPendingPrunedInputMemoryManager
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
        pending_manager = (
            ScopedPendingPrunedInputMemoryManager(
                MemoryLayerFactory._storage_factory(
                    registry, MemoryLayerName.PENDING, config.pending.scope
                ),
                config.pending,
            )
            if config.pending is not None and config.pending.enabled
            else None
        )
        return MemoryLayerSet(
            session=session_manager,
            archive=archive_manager,
            knowledge=knowledge_manager,
            pending=pending_manager,
        )

    @staticmethod
    def session_only(
        *,
        registry: MemoryStoreRegistry,
        config: SessionMemoryConfig | None = None,
        pending_config: PendingPrunedInputMemoryConfig | None = None,
    ) -> MemoryLayerSet:
        session_manager = ScopedSessionMemoryManager(
            MemoryLayerFactory._storage_factory(
                registry,
                MemoryLayerName.SESSION,
                (config or SessionMemoryConfig()).scope,
            ),
            config,
        )
        effective_pending_config = pending_config or PendingPrunedInputMemoryConfig()
        pending_manager = (
            ScopedPendingPrunedInputMemoryManager(
                MemoryLayerFactory._storage_factory(
                    registry,
                    MemoryLayerName.PENDING,
                    effective_pending_config.scope,
                ),
                effective_pending_config,
            )
            if effective_pending_config.enabled
            else None
        )
        return MemoryLayerSet(session=session_manager, pending=pending_manager)

    @staticmethod
    def subagent_session_isolated(
        registry: MemoryStoreRegistry,
        max_session_messages: int = 50,
    ) -> MemoryLayerSet:
        """Subagent memory: session-scoped archive, no knowledge layer.

        All layers use SessionScope to ensure complete isolation:
        - Session: SessionScope (unchanged)
        - Archive: SessionScope (NOT UserScope — each task session isolated)
        - Knowledge: disabled (None — no SOUL/USER/MEMORY.md access)
        """
        from framework.memory.layers.config import ArchiveMemoryConfig

        session_config = SessionMemoryConfig(max_messages=max_session_messages)
        archive_config = ArchiveMemoryConfig(scope=SessionScope())
        pending_config = PendingPrunedInputMemoryConfig(enabled=True)

        session_manager = ScopedSessionMemoryManager(
            MemoryLayerFactory._storage_factory(
                registry,
                MemoryLayerName.SESSION,
                session_config.scope,
            ),
            session_config,
        )
        archive_manager = ScopedArchiveMemoryManager(
            MemoryLayerFactory._storage_factory(
                registry,
                MemoryLayerName.ARCHIVE,
                archive_config.scope,
            ),
            archive_config,
        )
        pending_manager = ScopedPendingPrunedInputMemoryManager(
            MemoryLayerFactory._storage_factory(
                registry,
                MemoryLayerName.PENDING,
                pending_config.scope,
            ),
            pending_config,
        )
        return MemoryLayerSet(
            session=session_manager,
            archive=archive_manager,
            knowledge=None,
            pending=pending_manager,
        )

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
