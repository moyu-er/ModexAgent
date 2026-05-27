"""Memory system factory — creates MemorySystem from MemoryConfig."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from framework.core.provider import LLMProvider
from framework.ioc.configs.memory import MemoryConfig

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
        PendingPrunedInputMemoryConfig,
        SessionMemoryConfig,
    )

    pending_config = PendingPrunedInputMemoryConfig(
        enabled=cfg.pending.enabled,
        max_entries=cfg.pending.max_entries,
        max_chars=cfg.pending.max_chars,
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
        pending=pending_config,
    )


def create_memory(
    cfg: MemoryConfig,
    llm_provider: LLMProvider,
    workspace: Path,
) -> object:
    """Create a MemorySystem from config.

    Args:
        cfg: Memory configuration.
        llm_provider: LLMProvider for compression/summarization.
        workspace: Root directory for file-based storage.

    Returns:
        Initialized MemorySystem.
    """
    from framework.memory.system import create_memory_system

    layer_config = _build_memory_layer_config(cfg)

    archive_strategy = None
    if llm_provider is not None:
        from framework.agents.summarizer import SummarizerAgent
        from framework.memory.archive_generation import DualLLMArchiveGenerationStrategy

        summarizer = SummarizerAgent(llm_provider)
        archive_strategy = DualLLMArchiveGenerationStrategy(summarizer=summarizer)

    st = cfg.session
    cleanup_config: dict[str, int | float] = {
        "max_messages": st.max_messages,
        "max_tokens": st.max_tokens,
        "keep_ratio": st.keep_ratio_for_messages,
    }

    return create_memory_system(
        workspace=workspace,
        config=layer_config,
        llm_provider=llm_provider,
        archive_strategy=archive_strategy,
        cleanup_config=cleanup_config,
    )
