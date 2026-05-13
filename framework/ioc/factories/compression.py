"""Compression coordinator factory — builds DefaultMemoryCompressionCoordinator
from IOC MemoryConfig.
"""

from __future__ import annotations

from typing import Any

from framework.core.provider import LLMProvider
from framework.ioc.configs.memory import MemoryConfig


def create_compression_coordinator(
    cfg: MemoryConfig,
    llm_provider: LLMProvider,
) -> Any | None:
    """Build a compression coordinator from config.

    Returns None when both auto_llm_compression and auto_compact are disabled.
    """
    st = cfg.short_term
    if not st.auto_llm_compression:
        return None

    from framework.agents.summarizer import SummarizerAgent, SummarizerStrategy
    from framework.memory.compaction.boundary import (
        BoundaryPolicyName,
        create_boundary_policy,
    )
    from framework.memory.compaction.policy import ConservativeCompactionPolicy
    from framework.memory.compression.policies import DefaultMemoryCompressionCoordinator
    from framework.memory.retention import DefaultMessageRetentionPolicy

    summarizer = SummarizerAgent(llm_provider)
    summary_strategy = SummarizerStrategy(summarizer)

    compaction = ConservativeCompactionPolicy(high_value_tools=set())
    retention_policy = DefaultMessageRetentionPolicy.from_config({})
    boundary = create_boundary_policy(BoundaryPolicyName.TOOL_CHAIN)

    return DefaultMemoryCompressionCoordinator(
        summary=summary_strategy,
        compaction=compaction,
        boundary=boundary,
        retention=retention_policy,
        max_messages=st.max_messages,
        max_tokens=st.max_tokens,
        keep_ratio_for_messages=st.keep_ratio_for_messages,
        keep_ratio_for_token=st.keep_ratio_for_token,
    )


def create_peer_compression_coordinator(
    cfg: MemoryConfig | None,
) -> Any | None:
    """Build a lightweight compression coordinator for peers/subagents.

    Peers only get basic token/message thresholds; no LLM summarizer.
    """
    if cfg is None:
        return None

    st = cfg.short_term
    from framework.memory.compression.policies import DefaultMemoryCompressionCoordinator

    return DefaultMemoryCompressionCoordinator(
        max_messages=st.max_messages,
        max_tokens=st.max_tokens,
        keep_ratio_for_messages=st.keep_ratio_for_messages,
        keep_ratio_for_token=st.keep_ratio_for_token,
    )
