from __future__ import annotations

from typing import Any

from modex_agent.core.provider import LLMProvider
from modex_agent.memory.core.layers import MemoryLayerSet
from modex_agent.core.scope import (
    MemoryContext,
    MemoryLayerName,
    MemoryScope,
    SessionScope,
    UserScope,
)
from modex_agent.memory.core.storage import MemoryStorage
from modex_agent.memory.layers.archive import ScopedArchiveMemoryManager
from modex_agent.memory.layers.config import (
    ArchiveMemoryConfig,
    MemoryLayerConfigSet,
    SessionMemoryConfig,
    StorageFactory,
    UserRetentionBufferConfig,
)
from modex_agent.memory.layers.knowledge import ScopedKnowledgeMemoryManager
from modex_agent.memory.layers.session import ScopedSessionMemoryManager
from modex_agent.memory.layers.user_buffer import ScopedUserRetentionBuffer
from modex_agent.memory.registry import MemoryStoreRegistry


class MemoryLayerFactory:
    """Build typed layer sets using a MemoryStoreRegistry."""

    @staticmethod
    def build(
        *,
        registry: MemoryStoreRegistry,
        config: MemoryLayerConfigSet,
    ) -> MemoryLayerSet:
        """Build a MemoryLayerSet from a MemoryLayerConfigSet."""
        session_manager = ScopedSessionMemoryManager(
            MemoryLayerFactory._storage_factory(
                registry, MemoryLayerName.SESSION, config.session.scope
            ),
            config.session,
        )
        archive_manager = MemoryLayerFactory._maybe_build(
            registry, config.archive, MemoryLayerName.ARCHIVE, ScopedArchiveMemoryManager
        )
        knowledge_manager = MemoryLayerFactory._maybe_build(
            registry, config.knowledge, MemoryLayerName.KNOWLEDGE, ScopedKnowledgeMemoryManager
        )
        user_retention_manager = (
            MemoryLayerFactory._maybe_build(
                registry, config.user_retention, MemoryLayerName.USER_RETENTION, ScopedUserRetentionBuffer
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
    def _maybe_build(
        registry: MemoryStoreRegistry,
        config: Any | None,
        layer: MemoryLayerName,
        manager_cls: Any,
    ) -> Any:
        if config is None:
            return None
        return manager_cls(
            MemoryLayerFactory._storage_factory(registry, layer, config.scope),
            config,
        )

    @staticmethod
    def single_user(
        *,
        registry: MemoryStoreRegistry,
        config: MemoryLayerConfigSet | None = None,
        llm_provider: LLMProvider | None = None,
    ) -> MemoryLayerSet:
        _ = llm_provider
        config = config or MemoryLayerConfigSet()
        return MemoryLayerFactory.build(registry=registry, config=config)

    @staticmethod
    def session_only(
        *,
        registry: MemoryStoreRegistry,
        config: SessionMemoryConfig | None = None,
        user_retention_config: UserRetentionBufferConfig | None = None,
    ) -> MemoryLayerSet:
        effective_config = MemoryLayerConfigSet(
            session=config or SessionMemoryConfig(),
            archive=None,
            knowledge=None,
            user_retention=user_retention_config or UserRetentionBufferConfig(),
        )
        return MemoryLayerFactory.build(registry=registry, config=effective_config)

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
        effective_config = MemoryLayerConfigSet(
            session=SessionMemoryConfig(max_messages=max_session_messages),
            archive=ArchiveMemoryConfig(scope=SessionScope()),
            knowledge=None,
            user_retention=UserRetentionBufferConfig(enabled=True),
        )
        return MemoryLayerFactory.build(registry=registry, config=effective_config)

    @staticmethod
    def _storage_factory(
        registry: MemoryStoreRegistry,
        layer: MemoryLayerName,
        scope: MemoryScope | None = None,
    ) -> StorageFactory:
        effective_scope = scope or (
            SessionScope() if layer == MemoryLayerName.SESSION else UserScope()
        )

        async def resolve(context: MemoryContext) -> MemoryStorage:
            return await registry.resolve(layer=layer, scope=effective_scope, context=context)

        return resolve
