"""Memory-system factory — creates MemorySystem from MemoryConfig (W3a:
moved from the deleted ``ioc/factories/memory.py``; the special-agent
builders live in ``modex_agent.agents.summarizer.builders`` — the agents
package that owns the concrete summarizer agents)."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from modex_agent.agents.summarizer.builders import build_archive_agents, build_session_compactor
from modex_agent.core.provider import LLMProvider
from modex_agent.memory.config import MemoryConfig
from modex_agent.memory.default_system import DefaultMemorySystem
from modex_agent.memory.token_estimator import TokenEstimator

if TYPE_CHECKING:
    from modex_agent.memory.layers.config import MemoryLayerConfigSet
    from modex_agent.memory.registry import MemoryStoreRegistry


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
        from modex_agent.memory.layers.config import ArchiveMemoryConfig
        from modex_agent.memory.scope import build_scope

        archive_config = ArchiveMemoryConfig(
            max_entries=cfg.archive.max_entries,
            retained_consumed_archive_pairs=cfg.archive.retained_consumed_pairs,
            max_archive_total=cfg.archive.max_archive_total,
            scope=build_scope(cfg.archive.scope),
        )

    # Core memory config (new field, migrated from long_term if old config used)
    core_memory_config = None
    if cfg.core is not None and cfg.core.enabled:
        from modex_agent.memory.layers.config import CoreMemoryConfig
        from modex_agent.memory.scope import build_scope

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
    # The tail keep budget is the engine's absolute formula
    # clamp(usable×0.25, 2k..15k) (PRD §4.4.7) — deliberately not a config knob.
    cleanup_config: dict[str, int | float] = {
        "max_context_tokens": st.max_context_tokens,
        "max_token_ratio": st.max_token_ratio,
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

    # Summarizer-agent wiring (archive flow) — the special agents are built
    # by the memory domain's assembly face.
    archive_agent, core_memory_consolidator = build_archive_agents(cfg, llm_provider)
    archive_storage = None

    # Compact agent wiring — always enabled by default (compact_enabled=True).
    # Required for all agents (main + subagent): generates session-level
    # compact summary when token pressure triggers cleanup.
    compactor = build_session_compactor(cfg, llm_provider, token_estimator)

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
