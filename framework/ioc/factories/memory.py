"""Memory system factory — creates MemorySystem from MemoryConfig."""

from __future__ import annotations

from pathlib import Path

from framework.core.provider import LLMProvider
from framework.ioc.configs.memory import MemoryConfig


def _build_memory_layer_config(cfg: MemoryConfig) -> object:
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
    from framework.memory.lifecycle import DefaultMemoryLifecyclePolicy
    from framework.memory.system import create_memory_system

    layer_config = _build_memory_layer_config(cfg)

    compression_coordinator = None
    if cfg.short_term.auto_llm_compression:
        from framework.agents.summarizer import SummarizerAgent, SummarizerStrategy
        from framework.memory.compaction.boundary import (
            BoundaryPolicyName,
            create_boundary_policy,
        )
        from framework.memory.compaction.policy import ConservativeCompactionPolicy
        from framework.memory.compression.policies import (
            DefaultMemoryCompressionCoordinator,
        )
        from framework.memory.retention import DefaultMessageRetentionPolicy

        summarizer = SummarizerAgent(llm_provider)
        summary_strategy = SummarizerStrategy(summarizer)

        compression_coordinator = DefaultMemoryCompressionCoordinator(
            summary=summary_strategy,
            compaction=ConservativeCompactionPolicy(),
            retention=DefaultMessageRetentionPolicy.from_config({}),
            boundary=create_boundary_policy(BoundaryPolicyName.TOOL_CHAIN),
            max_messages=cfg.short_term.max_messages,
            max_tokens=cfg.short_term.max_tokens,
            keep_ratio_for_messages=cfg.short_term.keep_ratio_for_messages,
            keep_ratio_for_token=cfg.short_term.keep_ratio_for_token,
        )

    lifecycle = (
        DefaultMemoryLifecyclePolicy(compression_coordinator=compression_coordinator)
        if compression_coordinator
        else None
    )

    return create_memory_system(
        workspace=workspace,
        config=layer_config,
        llm_provider=llm_provider,
        lifecycle_policy=lifecycle,
    )
