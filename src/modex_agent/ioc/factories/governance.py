"""Governance factory — builds ContextGovernance from IOC MemoryConfig.

This replaces the hand-rolled CompositeGovernance assembly in BotService.
"""

from __future__ import annotations

from typing import Any

from modex_agent.ioc.configs.memory import GovernanceConfig, MemoryConfig


def create_governance(
    cfg: MemoryConfig | None,
) -> Any | None:
    """Build ContextGovernance chain from IOC config.

    Chain order: lossy_compaction → tool_chain_repair

    Args:
        cfg: Memory configuration (governance lives inside it).

    Returns:
        CompositeGovernance or None if disabled.
    """
    if cfg is None:
        return None

    _gov = cfg.governance
    if _gov is None or not _gov.tool_chain_repair:
        return None

    from modex_agent.memory.context_governance import (
        CompositeGovernance,
        LossyContentCompactionGovernance,
        ToolChainRepairGovernance,
    )

    strategies: list[Any] = []

    # Lossy compaction — truncates oversized content and tool-call arguments
    if _gov.lossy_compaction is not None:
        lc = _gov.lossy_compaction
        strategies.append(
            LossyContentCompactionGovernance(
                tool_result_head_chars=lc.tool_result_head_chars,
                assistant_head_chars=lc.assistant_head_chars,
                agent_head_chars=lc.agent_head_chars,
                user_head_chars=lc.user_head_chars,
                tool_args_head_chars=lc.tool_args_head_chars,
                compact_range_count=lc.compact_range_count,
            )
        )

    # Tool chain repair runs last (after compaction) so it can
    # clean up any structural issues before sending to the LLM.
    strategies.append(ToolChainRepairGovernance())
    return CompositeGovernance(strategies)


def create_subagent_governance(
    cfg: MemoryConfig | None,
) -> Any | None:
    """Build lightweight governance for subagents.

    Chain: ToolChainRepair only.
    No lossy compaction (that's main-agent only).
    """
    from modex_agent.memory.context_governance import (
        CompositeGovernance,
        ToolChainRepairGovernance,
    )

    if cfg is None or cfg.governance is None:
        _gov = GovernanceConfig()
    else:
        _gov = cfg.governance

    if not _gov.tool_chain_repair:
        return None

    strategies: list[Any] = [
        ToolChainRepairGovernance()
    ]
    return CompositeGovernance(strategies)
