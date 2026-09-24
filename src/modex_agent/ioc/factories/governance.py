"""Governance factory — builds ContextGovernance from IOC MemoryConfig.

This replaces the hand-rolled CompositeGovernance assembly in BotService.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from modex_agent.ioc.configs.memory import GovernanceConfig, MemoryConfig

if TYPE_CHECKING:
    from modex_agent.core.agent import AgentContext
    from modex_agent.memory.core.system import ContextManagedMemorySystem
    from modex_agent.memory.scope import MemoryContext


def _memory_compaction_strategy(
    cfg: MemoryConfig | None,
    memory_system: ContextManagedMemorySystem | None,
    memory_context_resolver: Callable[[AgentContext], MemoryContext] | None,
    token_estimator: Any | None,
) -> Any | None:
    """Build the MemoryCompactionGovernance strategy, or None when unwired.

    Requires BOTH the memory system and its context resolver: the resolver
    must return the same ``MemoryContext`` instance ``load()`` builds for the
    session (memory stores are keyed by context — a look-alike context would
    compact a different storage scope). Passing only one is a wiring bug and
    fails fast.

    The fallback budget AND the trigger ratio both come from
    ``cfg.session`` when present (``max_context_tokens`` /
    ``max_output_tokens`` / ``max_token_ratio``) so the read-side pre-check
    and the write-side trigger stay on one threshold. When the caller did
    not inject an explicit estimator but DID inject the memory system, the
    system's own estimator is reused — one estimator per deployment.
    """
    if memory_system is None and memory_context_resolver is None:
        return None
    if memory_system is None or memory_context_resolver is None:
        raise ValueError(
            "memory_system and memory_context_resolver must be provided "
            "together to enable MemoryCompactionGovernance"
        )
    from modex_agent.memory.compaction_governance import MemoryCompactionGovernance

    session_cfg = cfg.session if cfg is not None else None
    return MemoryCompactionGovernance(
        memory_system,
        memory_context_resolver,
        fallback_max_context_tokens=(
            session_cfg.max_context_tokens if session_cfg is not None else None
        ),
        fallback_max_output_tokens=(
            session_cfg.max_output_tokens if session_cfg is not None else 0
        ),
        trigger_ratio=(
            session_cfg.max_token_ratio if session_cfg is not None else 0.85
        ),
        token_estimator=(
            token_estimator if token_estimator is not None else memory_system.token_estimator
        ),
    )


def create_governance(
    cfg: MemoryConfig | None,
    token_estimator: Any | None = None,
    memory_system: ContextManagedMemorySystem | None = None,
    memory_context_resolver: Callable[[AgentContext], MemoryContext] | None = None,
) -> Any | None:
    """Build ContextGovernance chain from IOC config.

    Chain order: memory_compaction → context_budget → tool_chain_repair

    Memory compaction (persistent, LLM-summarized, ``session.max_token_ratio``)
    runs FIRST — it is
    the highest-fidelity face; mechanical budget placeholder pruning only
    serves deployments without a memory system or with an explicit budget
    config; the reactive emergency fallback lives in the llm_client, outside
    this chain.

    Args:
        cfg: Memory configuration (governance lives inside it).
        token_estimator: Token estimator to inject into the token-aware
            strategies. When ``None`` and a ``memory_system`` is wired, the
            memory strategy reuses the system's own estimator; otherwise
            strategies fall back to ``CharTokenEstimator``.
        memory_system: Memory system driving persistent compaction; ``None``
            skips the memory strategy (memory-less deployments).
        memory_context_resolver: Resolves the turn's AgentContext to the
            MemoryContext ``load()`` built for that session. Required
            together with ``memory_system``.

    Returns:
        CompositeGovernance or None if no strategy is enabled.
    """
    _gov = cfg.governance if cfg is not None else None

    # Memory-less deployments keep the exact legacy gate: the chain exists
    # only when governance is configured with tool_chain_repair on.
    memory_wired = memory_system is not None or memory_context_resolver is not None
    if not memory_wired and (_gov is None or not _gov.tool_chain_repair):
        return None

    strategies: list[Any] = []

    # Memory compaction governance — persistent compaction, chain head.
    memory_strategy = _memory_compaction_strategy(
        cfg, memory_system, memory_context_resolver, token_estimator
    )
    if memory_strategy is not None:
        strategies.append(memory_strategy)

    if _gov is not None and _gov.budget is not None:
        from modex_agent.memory.context_governance import ContextBudgetGovernance

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

    # Tool chain repair runs last (after pruning/compaction) so it can
    # clean up any structural issues before sending to the LLM.
    if _gov is not None and _gov.tool_chain_repair:
        from modex_agent.memory.context_governance import ToolChainRepairGovernance

        strategies.append(ToolChainRepairGovernance())

    if not strategies:
        return None
    from modex_agent.memory.context_governance import CompositeGovernance

    return CompositeGovernance(strategies)


def create_subagent_governance(
    cfg: MemoryConfig | None,
    memory_system: ContextManagedMemorySystem | None = None,
    memory_context_resolver: Callable[[AgentContext], MemoryContext] | None = None,
) -> Any | None:
    """Build governance for subagents.

    Chain: memory_compaction (when wired) → tool_chain_repair.

    Same chain shape as the main agent (convergence rule 1): a subagent with
    a memory system gets the same persistent-compaction head; without one
    (the plain framework default) only tool-chain repair runs — subagent
    budget pruning stays off (short-lived, small context).
    """
    _gov = GovernanceConfig() if cfg is None or cfg.governance is None else cfg.governance

    strategies: list[Any] = []

    memory_strategy = _memory_compaction_strategy(cfg, memory_system, memory_context_resolver, None)
    if memory_strategy is not None:
        strategies.append(memory_strategy)

    if _gov.tool_chain_repair:
        from modex_agent.memory.context_governance import ToolChainRepairGovernance

        strategies.append(ToolChainRepairGovernance())

    if not strategies:
        return None
    from modex_agent.memory.context_governance import CompositeGovernance

    return CompositeGovernance(strategies)
