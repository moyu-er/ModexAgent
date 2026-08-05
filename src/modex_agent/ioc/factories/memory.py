"""Memory system factory — creates MemorySystem from MemoryConfig."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from modex_agent.core.provider import LLMProvider
from modex_agent.ioc.configs.memory import MemoryConfig
from modex_agent.memory.default_system import DefaultMemorySystem
from modex_agent.memory.token_estimator import TokenEstimator

if TYPE_CHECKING:
    from modex_agent.memory.layers.config import MemoryLayerConfigSet
    from modex_agent.memory.registry import MemoryStoreRegistry

logger = logging.getLogger(__name__)


def _build_memory_layer_config(cfg: MemoryConfig) -> MemoryLayerConfigSet:
    """Convert MemoryConfig to framework MemoryLayerConfigSet.

    Supports both old (short_term/long_term) and new (session/archive/core) config.
    Migration happens in MemoryConfig.model_post_init, so this function
    only reads from the new fields.
    """
    from modex_agent.memory.layers.config import (
        MemoryLayerConfigSet,
        SessionMemoryConfig,
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
            max_archive_total=cfg.archive.max_archive_total,
            scope=build_scope(cfg.archive.scope),
        )

    # Core memory config (new field, migrated from long_term if old config used)
    core_memory_config = None
    if cfg.core is not None and cfg.core.enabled:
        from modex_agent.core.scope import build_scope
        from modex_agent.memory.layers.config import CoreMemoryConfig

        core_memory_config = CoreMemoryConfig(
            default_templates_dir=cfg.core.default_templates_dir,
            scope=build_scope(cfg.core.scope),
        )

    return MemoryLayerConfigSet(
        session=session_config,
        archive=archive_config,
        core=core_memory_config,
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
        "max_output_tokens": st.max_output_tokens,
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

    # Summarizer-agent wiring (archive flow)
    archive_agent = None
    archive_storage = None
    core_memory_consolidator = None

    archive_enabled = cfg.archive is not None and cfg.archive.enabled
    summarizer_enabled = cfg.summarizer_agent is not None and cfg.summarizer_agent.enabled

    if archive_enabled or summarizer_enabled:
        from modex_agent.agents.summarizer.archive_agent import (
            ArchiveSummarizer,
            ArchiveSummarizerConfig,
        )
        from modex_agent.agents.summarizer.consolidator import CoreMemoryConsolidator

        if cfg.summarizer_agent is not None:
            archive_config = ArchiveSummarizerConfig(
                context_max_chars=cfg.summarizer_agent.context_max_chars,
                core_max_chars=cfg.summarizer_agent.core_max_chars,
                max_iterations=cfg.summarizer_agent.max_iterations,
            )
            max_iterations = cfg.summarizer_agent.max_iterations
        else:
            archive_config = ArchiveSummarizerConfig()
            max_iterations = ArchiveSummarizerConfig().max_iterations

        if llm_provider is None:
            logger.warning(
                "llm_provider is None — skipping archive summarizer and "
                "core memory consolidator (no model configured). Memory runs "
                "in degraded mode until a model is configured."
            )
        else:
            archive_agent = ArchiveSummarizer(llm_provider, config=archive_config)

            core_enabled = cfg.core is not None and cfg.core.enabled
            if core_enabled:
                core_memory_consolidator = CoreMemoryConsolidator(
                    provider=llm_provider,
                    max_iterations=max_iterations,
                )

    # Compact agent wiring — always enabled by default (compact_enabled=True).
    # Required for all agents (main + subagent): generates session-level
    # compact summary when token pressure triggers cleanup.
    compactor = None
    compact_enabled = cfg.compact is not None and cfg.compact.enabled
    if compact_enabled:
        if llm_provider is None:
            logger.warning(
                "llm_provider is None — skipping session compactor "
                "(no model configured). Cleanup will run in degraded mode "
                "(tail-only, no compact summary)."
            )
        else:
            from modex_agent.agents.summarizer.session_compactor import (
                SessionCompactorAgent,
                SessionCompactorConfig,
            )

            compact_cfg = SessionCompactorConfig(
                max_output_tokens=cfg.compact.max_output_tokens,
                max_iterations=cfg.compact.max_iterations,
                temperature=cfg.compact.temperature,
                tool_output_max_chars=cfg.compact.tool_output_max_chars,
            )
            compactor = SessionCompactorAgent(llm_provider, config=compact_cfg)

    return create_memory_system(
        workspace=workspace,
        config=layer_config,
        llm_provider=llm_provider,
        cleanup_config=cleanup_config,
        pruned_manager=pruned_manager,
        archive_agent=archive_agent,
        archive_storage=archive_storage,
        core_memory_consolidator=core_memory_consolidator,
        token_estimator=token_estimator,
        store_registry=store_registry,
        compactor=compactor,
    )
