"""Memory system factory — creates MemorySystem from MemoryConfig."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from modex_agent.core.provider import LLMProvider
from modex_agent.ioc.configs.memory import MemoryConfig
from modex_agent.memory.default_system import DefaultMemorySystem
from modex_agent.memory.token_estimator import TokenEstimator

if TYPE_CHECKING:
    from modex_agent.memory.layers.config import MemoryLayerConfigSet
    from modex_agent.memory.registry import MemoryStoreRegistry


def _build_memory_layer_config(cfg: MemoryConfig) -> MemoryLayerConfigSet:
    """Convert MemoryConfig to framework MemoryLayerConfigSet.

    Supports both old (short_term/long_term) and new (session/archive/knowledge) config.
    Migration happens in MemoryConfig.model_post_init, so this function
    only reads from the new fields.
    """
    from modex_agent.memory.layers.config import (
        MemoryLayerConfigSet,
        SessionMemoryConfig,
        UserRetentionBufferConfig,
    )

    user_retention_config = UserRetentionBufferConfig(
        enabled=cfg.user_retention.enabled,
        max_entries=cfg.user_retention.max_entries,
        max_user_chars=cfg.user_retention.max_user_chars,
        max_assistant_chars=cfg.user_retention.max_assistant_chars,
    )

    session_config = SessionMemoryConfig()

    # Archive config (new field, migrated from long_term if old config used)
    archive_config = None
    if cfg.archive is not None and cfg.archive.enabled:
        from modex_agent.core.scope import build_scope
        from modex_agent.memory.layers.config import ArchiveMemoryConfig

        archive_config = ArchiveMemoryConfig(
            max_entries=cfg.archive.max_entries,
            retained_consumed_archive_pairs=cfg.archive.retained_consumed_pairs,
            scope=build_scope(cfg.archive.scope),
        )

    # Knowledge config (new field, migrated from long_term if old config used)
    knowledge_config = None
    if cfg.knowledge is not None and cfg.knowledge.enabled:
        from modex_agent.core.scope import build_scope
        from modex_agent.memory.layers.config import KnowledgeMemoryConfig

        knowledge_config = KnowledgeMemoryConfig(
            default_templates_dir=cfg.knowledge.default_templates_dir,
            scope=build_scope(cfg.knowledge.scope),
        )

    return MemoryLayerConfigSet(
        session=session_config,
        archive=archive_config,
        knowledge=knowledge_config,
        user_retention=user_retention_config,
    )


def create_memory(
    cfg: MemoryConfig,
    llm_provider: LLMProvider | None,
    workspace: Path,
    token_estimator: TokenEstimator | None = None,
    store_registry: MemoryStoreRegistry | None = None,
) -> DefaultMemorySystem:
    """Create a MemorySystem from config.

    Args:
        cfg: Memory configuration.
        llm_provider: LLMProvider for compression/summarization.
        workspace: Root directory for file-based storage.
        token_estimator: Optional token estimator (defaults to char-based).
        store_registry: Optional storage registry; defaults to file-backed storage.

    Returns:
        Initialized DefaultMemorySystem.
    """
    from modex_agent.memory.system import create_memory_system

    layer_config = _build_memory_layer_config(cfg)

    st = cfg.session
    cleanup_config: dict[str, int | float] = {
        "max_context_tokens": st.max_context_tokens,
        "max_token_ratio": st.max_token_ratio,
        "keep_ratio": st.keep_ratio,
    }

    # Pruned catalog manager (independent of archive)
    pruned_manager = None
    if cfg.pruned is not None and cfg.pruned.enabled:
        from modex_agent.memory.pruned.manager import PrunedManager

        pruned_manager = PrunedManager(
            pruned_base_dir=workspace / "pruned",
            max_files=cfg.pruned.max_files,
            topic_max_chars=cfg.pruned.topic_max_chars,
        )

    # Summarizer-agent wiring (new agent-based archive flow)
    # Archive generation is enabled whenever the archive layer is enabled.
    # Explicit summarizer_agent config overrides defaults.
    archive_agent = None
    archive_storage = None
    knowledge_consolidator = None

    archive_enabled = cfg.archive is not None and cfg.archive.enabled
    summarizer_enabled = cfg.summarizer_agent is not None and cfg.summarizer_agent.enabled

    if archive_enabled or summarizer_enabled:
        from modex_agent.agents.summarizer.archive_agent import (
            ArchiveSummarizer,
            ArchiveSummarizerConfig,
        )
        from modex_agent.agents.summarizer.consolidator import KnowledgeConsolidator

        if cfg.summarizer_agent is not None:
            archive_config = ArchiveSummarizerConfig(
                context_max_chars=cfg.summarizer_agent.context_max_chars,
                knowledge_max_chars=cfg.summarizer_agent.knowledge_max_chars,
                index_max_chars=cfg.summarizer_agent.index_max_chars,
                max_iterations=cfg.summarizer_agent.max_iterations,
            )
            max_iterations = cfg.summarizer_agent.max_iterations
        else:
            archive_config = ArchiveSummarizerConfig()
            max_iterations = ArchiveSummarizerConfig().max_iterations

        archive_agent = ArchiveSummarizer(llm_provider, config=archive_config)

        # Knowledge consolidator is created when knowledge layer is enabled
        knowledge_enabled = cfg.knowledge is not None and cfg.knowledge.enabled
        if knowledge_enabled:
            knowledge_consolidator = KnowledgeConsolidator(
                provider=llm_provider,
                max_iterations=max_iterations,
            )

        # archive_storage is created dynamically in cleanup_session
        # via archive.get_storage_path(context) — not hardcoded here

    return create_memory_system(
        workspace=workspace,
        config=layer_config,
        llm_provider=llm_provider,
        cleanup_config=cleanup_config,
        pruned_manager=pruned_manager,
        archive_agent=archive_agent,
        archive_storage=archive_storage,
        knowledge_consolidator=knowledge_consolidator,
        token_estimator=token_estimator,
        store_registry=store_registry,
    )
