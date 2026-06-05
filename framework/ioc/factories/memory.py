"""Memory system factory — creates MemorySystem from MemoryConfig."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from framework.core.provider import LLMProvider
from framework.ioc.configs.memory import MemoryConfig
from framework.memory.default_system import DefaultMemorySystem

if TYPE_CHECKING:
    from framework.memory.layers.config import MemoryLayerConfigSet


def _build_memory_layer_config(cfg: MemoryConfig) -> MemoryLayerConfigSet:
    """Convert MemoryConfig to framework MemoryLayerConfigSet.

    Supports both old (short_term/long_term) and new (session/archive/knowledge) config.
    Migration happens in MemoryConfig.model_post_init, so this function
    only reads from the new fields.
    """
    from framework.memory.layers.config import (
        MemoryLayerConfigSet,
        UserRetentionBufferConfig,
        SessionMemoryConfig,
    )

    user_retention_config = UserRetentionBufferConfig(
        enabled=cfg.user_retention.enabled,
        max_entries=cfg.user_retention.max_entries,
        max_user_chars=cfg.user_retention.max_user_chars,
        max_assistant_chars=cfg.user_retention.max_assistant_chars,
    )

    session_config = SessionMemoryConfig(
        max_messages=cfg.session.max_messages,
    )

    # Archive config (new field, migrated from long_term if old config used)
    archive_config = None
    if cfg.archive is not None and cfg.archive.enabled:
        from framework.memory.layers.config import ArchiveMemoryConfig

        archive_config = ArchiveMemoryConfig(
            max_entries=cfg.archive.max_entries,
            retained_consumed_archive_pairs=cfg.archive.retained_consumed_pairs,
        )

    # Knowledge config (new field, migrated from long_term if old config used)
    knowledge_config = None
    if cfg.knowledge is not None and cfg.knowledge.enabled:
        from framework.memory.layers.config import KnowledgeMemoryConfig

        knowledge_config = KnowledgeMemoryConfig(
            default_templates_dir=cfg.knowledge.default_templates_dir,
        )

    return MemoryLayerConfigSet(
        session=session_config,
        archive=archive_config,
        knowledge=knowledge_config,
        user_retention=user_retention_config,
    )


def create_memory(
    cfg: MemoryConfig,
    llm_provider: LLMProvider,
    workspace: Path,
) -> DefaultMemorySystem:
    """Create a MemorySystem from config.

    Args:
        cfg: Memory configuration.
        llm_provider: LLMProvider for compression/summarization.
        workspace: Root directory for file-based storage.

    Returns:
        Initialized DefaultMemorySystem.
    """
    from framework.memory.system import create_memory_system

    layer_config = _build_memory_layer_config(cfg)

    st = cfg.session
    cleanup_config: dict[str, int | float] = {
        "max_messages": st.max_messages,
        "max_tokens": st.max_tokens,
        "keep_ratio": st.keep_ratio_for_messages,
    }

    # Pruned catalog manager (independent of archive)
    pruned_manager = None
    if cfg.pruned is not None and cfg.pruned.enabled:
        from framework.memory.pruned.manager import PrunedManager

        pruned_manager = PrunedManager(
            pruned_base_dir=workspace / "pruned",
            max_files=cfg.pruned.max_files,
            topic_max_chars=cfg.pruned.topic_max_chars,
        )

    # Summarizer-agent wiring (new agent-based archive flow)
    archive_agent = None
    archive_storage = None
    knowledge_consolidator = None

    if cfg.summarizer_agent is not None and cfg.summarizer_agent.enabled:
        from framework.agents.summarizer.archive_agent import (
            ArchiveSummarizer,
            ArchiveSummarizerConfig,
        )
        from framework.agents.summarizer.consolidator import KnowledgeConsolidator

        archive_config = ArchiveSummarizerConfig(
            context_max_chars=cfg.summarizer_agent.context_max_chars,
            knowledge_max_chars=cfg.summarizer_agent.knowledge_max_chars,
            index_max_chars=cfg.summarizer_agent.index_max_chars,
            max_iterations=cfg.summarizer_agent.max_iterations,
        )
        archive_agent = ArchiveSummarizer(llm_provider, config=archive_config)

        consolidator = KnowledgeConsolidator(
            provider=llm_provider,
            max_iterations=cfg.summarizer_agent.max_iterations,
        )

        # archive_storage is created dynamically in cleanup_session
        # via archive.get_storage_path(context) — not hardcoded here

        knowledge_consolidator = consolidator

    return create_memory_system(
        workspace=workspace,
        config=layer_config,
        llm_provider=llm_provider,
        cleanup_config=cleanup_config,
        pruned_manager=pruned_manager,
        archive_agent=archive_agent,
        archive_storage=archive_storage,
        knowledge_consolidator=knowledge_consolidator,
    )
