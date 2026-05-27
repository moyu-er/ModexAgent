"""Memory system factory — creates MemorySystem from MemoryConfig."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from framework.core.provider import LLMProvider
from framework.ioc.configs.memory import MemoryConfig

if TYPE_CHECKING:
    from framework.memory.layers.config import MemoryLayerConfigSet


def _build_memory_layer_config(cfg: MemoryConfig) -> MemoryLayerConfigSet:
    """Convert MemoryConfig to framework MemoryLayerConfigSet."""
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
        max_messages=cfg.short_term.max_messages,
    )

    archive_config = None
    knowledge_config = None
    if cfg.long_term is not None and cfg.long_term.enabled:
        from framework.memory.layers.config import (
            ArchiveMemoryConfig,
            KnowledgeMemoryConfig,
        )

        archive_config = ArchiveMemoryConfig()
        knowledge_config = KnowledgeMemoryConfig()

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

    st = cfg.short_term
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
