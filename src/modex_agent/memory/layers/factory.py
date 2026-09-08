from __future__ import annotations

from typing import Any

from modex_agent.core.provider import LLMProvider
from modex_agent.memory.core.layers import MemoryLayerSet
from modex_agent.memory.core.split_stores import MemoryStoreBundle
from modex_agent.memory.hooks import MemoryHookRunner
from modex_agent.memory.layers.archive import ScopedArchiveMemoryManager
from modex_agent.memory.layers.config import (
    ArchiveMemoryConfig,
    MemoryLayerConfigSet,
    SessionMemoryConfig,
    StorageFactory,
)
from modex_agent.memory.layers.core import ScopedCoreMemoryManager
from modex_agent.memory.layers.session import ScopedSessionMemoryManager
from modex_agent.memory.registry import MemoryStoreRegistry
from modex_agent.memory.scope import (
    MemoryContext,
    MemoryLayerName,
    Scope,
    SessionScope,
    UserScope,
)
from modex_agent.memory.token_estimator import TokenEstimator


class MemoryLayerFactory:
    """Build typed layer sets using a MemoryStoreRegistry."""

    @staticmethod
    def build(
        *,
        registry: MemoryStoreRegistry,
        config: MemoryLayerConfigSet,
        hook_runner: MemoryHookRunner | None = None,
        token_estimator: TokenEstimator | None = None,
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
        core_memory_manager = (
            ScopedCoreMemoryManager(
                MemoryLayerFactory._storage_factory(
                    registry,
                    MemoryLayerName.CORE,
                    config.core.scope,
                ),
                config.core,
                hook_runner=hook_runner,
                token_estimator=token_estimator,
            )
            if config.core is not None
            else None
        )
        return MemoryLayerSet(
            session=session_manager,
            archive=archive_manager,
            core=core_memory_manager,
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
        hook_runner: MemoryHookRunner | None = None,
        token_estimator: TokenEstimator | None = None,
    ) -> MemoryLayerSet:
        _ = llm_provider
        config = config or MemoryLayerConfigSet()
        return MemoryLayerFactory.build(
            registry=registry,
            config=config,
            hook_runner=hook_runner,
            token_estimator=token_estimator,
        )

    @staticmethod
    def session_only(
        *,
        registry: MemoryStoreRegistry,
        config: SessionMemoryConfig | None = None,
    ) -> MemoryLayerSet:
        effective_config = MemoryLayerConfigSet(
            session=config or SessionMemoryConfig(),
            archive=None,
            core=None,
        )
        return MemoryLayerFactory.build(registry=registry, config=effective_config)

    @staticmethod
    def subagent_session_isolated(
        registry: MemoryStoreRegistry,
    ) -> MemoryLayerSet:
        """Subagent memory: session-scoped archive, no core memory layer.

        All layers use SessionScope to ensure complete isolation:
        - Session: SessionScope (unchanged)
        - Archive: SessionScope (NOT UserScope — each task session isolated)
        - Core memory: disabled (None — no SOUL/USER/MEMORY.md access)
        """
        effective_config = MemoryLayerConfigSet(
            session=SessionMemoryConfig(),
            archive=ArchiveMemoryConfig(scope=SessionScope()),
            core=None,
        )
        return MemoryLayerFactory.build(registry=registry, config=effective_config)

    @staticmethod
    def _storage_factory(
        registry: MemoryStoreRegistry,
        layer: MemoryLayerName,
        scope: Scope | None = None,
    ) -> StorageFactory:
        effective_scope = scope or (
            SessionScope() if layer == MemoryLayerName.SESSION else UserScope()
        )

        async def resolve(context: MemoryContext) -> MemoryStoreBundle:
            return await registry.resolve(layer=layer, scope=effective_scope, context=context)

        return resolve
