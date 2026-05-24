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
    from framework.memory.lifecycle import DefaultMemoryLifecyclePolicy
    from framework.memory.system import create_memory_system

    layer_config = _build_memory_layer_config(cfg)

    from framework.memory.compaction.boundary import (
        BoundaryPolicyName,
        create_boundary_policy,
    )
    from framework.memory.compaction.policy import ConservativeCompactionPolicy
    from framework.memory.compression.policies import (
        DefaultMemoryCompressionCoordinator,
    )
    from framework.memory.retention import DefaultMessageRetentionPolicy

    st = cfg.short_term

    archive_generation = None
    if st.auto_compact:
        from framework.agents.summarizer import SummarizerAgent
        from framework.memory.archive_generation import DualLLMArchiveGenerationStrategy

        summarizer = SummarizerAgent(llm_provider)
        archive_generation = DualLLMArchiveGenerationStrategy(summarizer=summarizer)

    compression_coordinator = DefaultMemoryCompressionCoordinator(
        archive_generation=archive_generation,
        compaction=ConservativeCompactionPolicy(),
        retention=DefaultMessageRetentionPolicy.from_config({}),
        boundary=create_boundary_policy(BoundaryPolicyName.TOOL_CHAIN),
        max_messages=st.max_messages,
        max_tokens=st.max_tokens,
        keep_ratio_for_messages=st.keep_ratio_for_messages,
        keep_ratio_for_token=st.keep_ratio_for_token,
    )

    lifecycle = DefaultMemoryLifecyclePolicy(compression_coordinator=compression_coordinator)

    return create_memory_system(
        workspace=workspace,
        config=layer_config,
        llm_provider=llm_provider,
        lifecycle_policy=lifecycle,
    )
