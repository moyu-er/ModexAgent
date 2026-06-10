"""Factory for default registry-backed memory layer sets."""

from __future__ import annotations

from framework.core.provider import LLMProvider
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
    SessionMemoryConfig,
    StorageFactory,
    UserRetentionBufferConfig,
)
from framework.memory.layers.knowledge import ScopedKnowledgeMemoryManager
from framework.memory.layers.session import ScopedSessionMemoryManager
from framework.memory.layers.user_buffer import ScopedUserRetentionBuffer
from framework.memory.registry import MemoryStoreRegistry


class MemoryLayerFactory:
    """Build typed layer sets using a MemoryStoreRegistry."""

    @staticmethod
    def single_user(
        *,
        registry: MemoryStoreRegistry,
        config: MemoryLayerConfigSet | None = None,
        llm_provider: LLMProvider | None = None,
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
        user_retention_manager = (
            ScopedUserRetentionBuffer(
                MemoryLayerFactory._storage_factory(
                    registry, MemoryLayerName.USER_RETENTION, config.user_retention.scope
                ),
                config.user_retention,
            )
            if config.user_retention is not None and config.user_retention.enabled
            else None
        )
        return MemoryLayerSet(
            session=session_manager,
            archive=archive_manager,
            knowledge=knowledge_manager,
            user_retention=user_retention_manager,
        )

    @staticmethod
    def session_only(
        *,
        registry: MemoryStoreRegistry,
        config: SessionMemoryConfig | None = None,
        user_retention_config: UserRetentionBufferConfig | None = None,
    ) -> MemoryLayerSet:
        session_manager = ScopedSessionMemoryManager(
            MemoryLayerFactory._storage_factory(
                registry,
                MemoryLayerName.SESSION,
                (config or SessionMemoryConfig()).scope,
            ),
            config,
        )
        effective_user_retention_config = user_retention_config or UserRetentionBufferConfig()
        user_retention_manager = (
            ScopedUserRetentionBuffer(
                MemoryLayerFactory._storage_factory(
                    registry,
                    MemoryLayerName.USER_RETENTION,
                    effective_user_retention_config.scope,
                ),
                effective_user_retention_config,
            )
            if effective_user_retention_config.enabled
            else None
        )
        return MemoryLayerSet(session=session_manager, user_retention=user_retention_manager)

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
        user_retention_config = UserRetentionBufferConfig(enabled=True)

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
        user_retention_manager = ScopedUserRetentionBuffer(
            MemoryLayerFactory._storage_factory(
                registry,
                MemoryLayerName.USER_RETENTION,
                user_retention_config.scope,
            ),
            user_retention_config,
        )
        return MemoryLayerSet(
            session=session_manager,
            archive=archive_manager,
            knowledge=None,
            user_retention=user_retention_manager,
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
