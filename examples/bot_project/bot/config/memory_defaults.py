"""Baked memory presets — not user-editable.

Two distinct presets: main-rich (long-term memory layers, used by main agents)
and sub-minimal (session-only + pruned + tool_chain_repair, used by subagents).
"""

from __future__ import annotations

from modex_agent.ioc.configs.memory import (
    ArchiveConfig,
    DreamEngineConfig,
    GovernanceConfig,
    KnowledgeConfig,
    LossyConfig,
    MemoryConfig,
    PrunedCatalogConfig,
    SessionConfig,
)


def main_agent_memory() -> MemoryConfig:
    """Canonical main-agent memory (long-term layers on).

    Values mirror ``config/pools/main/pool.yml`` ``memory:`` block verbatim.
    """
    return MemoryConfig(
        session=SessionConfig(max_token_ratio=0.85, keep_ratio=0.3),
        archive=ArchiveConfig(
            enabled=True,
            scope="global",
            max_archive_count=10,
            max_archive_total=20,
            max_archive_inject=3,
        ),
        knowledge=KnowledgeConfig(
            enabled=True,
            default_templates_dir="templates/knowledge",
            scope="global",
        ),
        dream_engine=DreamEngineConfig(
            enabled=True,
            interval=600,
            max_consume_per_run=3,
        ),
        governance=GovernanceConfig(
            tool_chain_repair=True,
            lossy_compaction=LossyConfig(
                tool_result_head_chars=1200,
                assistant_head_chars=1200,
                agent_head_chars=2000,
                user_head_chars=4000,
            ),
        ),
        pruned=PrunedCatalogConfig(enabled=True, max_files=50, topic_max_chars=200),
    )


def subagent_memory() -> MemoryConfig:
    """Subagent memory: session-only + pruned + tool_chain_repair, no long-term."""
    return MemoryConfig(
        session=SessionConfig(max_token_ratio=0.85, keep_ratio=0.3),
        archive=None,
        knowledge=None,
        governance=GovernanceConfig(tool_chain_repair=True),
        pruned=PrunedCatalogConfig(enabled=True, max_files=50, topic_max_chars=200),
    )
