"""Governance factory — builds ContextGovernance from IOC MemoryConfig.

This replaces the hand-rolled CompositeGovernance assembly in BotService.
"""

from __future__ import annotations

from typing import Any

from framework.ioc.configs.memory import GovernanceConfig, MemoryConfig, TokenBudgetConfig


def create_governance(
    cfg: MemoryConfig | None,
    llm_max_tokens: int = 80000,
) -> Any | None:
    """Build ContextGovernance chain from IOC config.

    Args:
        cfg: Memory configuration (governance lives inside it).
        llm_max_tokens: Max tokens from LLM config for budget calculations.

    Returns:
        CompositeGovernance or None if disabled.
    """
    if cfg is None:
        return None

    _gov = cfg.governance
    if _gov is None or not _gov.tool_chain_repair:
        return None

    from framework.memory.context_governance import (
        CompositeGovernance,
        FinalContextLegalityGovernance,
        LossyContentCompactionGovernance,
        PriorityBudgetGovernance,
        ToolChainRepairGovernance,
    )
    from framework.memory.retention import DefaultMessageRetentionPolicy

    strategies: list[Any] = [ToolChainRepairGovernance()]

    # Token budget
    if _gov.token_budget is not None:
        tb = _gov.token_budget
        retention_policy = DefaultMessageRetentionPolicy.from_config({})
        strategies.append(
            PriorityBudgetGovernance(
                max_tokens=min(int(llm_max_tokens * tb.budget_ratio), 128000),
                safety_buffer=tb.safety_buffer,
                retention_policy=retention_policy,
            )
        )

    # Lossy compaction
    if _gov.lossy_compaction is not None:
        lc = _gov.lossy_compaction
        strategies.append(
            LossyContentCompactionGovernance(
                tool_result_head_chars=lc.tool_result_head_chars,
                assistant_head_chars=lc.assistant_head_chars,
            )
        )

    strategies.append(FinalContextLegalityGovernance())
    return CompositeGovernance(strategies)


def create_peer_governance(
    cfg: MemoryConfig | None,
    llm_max_tokens: int = 80000,
) -> Any | None:
    """Build lightweight governance for peers/subagents.

    Uses sensible defaults when no governance config is set:
    ToolChainRepair + PriorityBudget(0.3 ratio, 64k cap) + FinalContextLegality.
    No lossy compaction (that's main-agent only).
    """
    from framework.memory.context_governance import (
        CompositeGovernance,
        FinalContextLegalityGovernance,
        PriorityBudgetGovernance,
        ToolChainRepairGovernance,
    )
    from framework.memory.retention import DefaultMessageRetentionPolicy

    if cfg is None or cfg.governance is None:
        _gov = GovernanceConfig(
            token_budget=TokenBudgetConfig(budget_ratio=0.3, safety_buffer=512),
        )
    else:
        _gov = cfg.governance

    if not _gov.tool_chain_repair:
        return None

    strategies: list[Any] = [ToolChainRepairGovernance()]

    if _gov.token_budget is not None:
        tb = _gov.token_budget
        budget_ratio = getattr(tb, "budget_ratio", 0.3)
        strategies.append(
            PriorityBudgetGovernance(
                max_tokens=min(int(llm_max_tokens * budget_ratio), 64000),
                safety_buffer=tb.safety_buffer,
                retention_policy=DefaultMessageRetentionPolicy(),
            )
        )

    strategies.append(FinalContextLegalityGovernance())
    return CompositeGovernance(strategies)
