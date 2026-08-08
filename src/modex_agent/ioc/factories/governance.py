"""Governance factory — builds ContextGovernance from IOC MemoryConfig.

This replaces the hand-rolled CompositeGovernance assembly in BotService.
"""

from __future__ import annotations

from typing import Any

from modex_agent.ioc.configs.memory import GovernanceConfig, MemoryConfig


def create_governance(
    cfg: MemoryConfig | None,
    token_estimator: Any | None = None,
) -> Any | None:
    """Build ContextGovernance chain from IOC config.

    Chain order: context_budget → tool_chain_repair

    Args:
        cfg: Memory configuration (governance lives inside it).
        token_estimator: Token estimator to inject into ContextBudgetGovernance.
            When ``None`` the governance uses ``CharTokenEstimator`` internally.

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
        ContextBudgetGovernance,
        ToolChainRepairGovernance,
    )

    strategies: list[Any] = []

    # Context budget governance — token-window tool-result pruning
    if _gov.budget is not None:
        b = _gov.budget
        max_ctx = cfg.session.max_context_tokens if cfg.session else 200_000
        strategies.append(
            ContextBudgetGovernance(
                max_context_tokens=max_ctx,
                token_estimator=token_estimator,
                governance_ratio=b.governance_ratio,
                protect_tokens=b.protect_tokens,
                min_gain_tokens=b.min_gain_tokens,
                keep_recent=b.keep_recent,
                whitelist_tools=frozenset(b.whitelist_tools) if b.whitelist_tools else None,
            )
        )

    # Tool chain repair runs last (after pruning) so it can
    # clean up any structural issues before sending to the LLM.
    strategies.append(ToolChainRepairGovernance())
    return CompositeGovernance(strategies)


def create_subagent_governance(
    cfg: MemoryConfig | None,
) -> Any | None:
    """Build lightweight governance for subagents.

    Chain: ToolChainRepair only.
    No budget pruning (subagents are short-lived, small context).
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
