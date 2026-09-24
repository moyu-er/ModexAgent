"""Pre-LLM memory-compaction governance (per-model context compaction PRD §4.3.2).

The read-side trigger face of the unified session-compaction interface: on
every LLM iteration it checks the assembled model-visible payload against the
CURRENT turn's effective budget and, when over pressure, drives the memory
system's persistent compaction (``compact_session``) instead of mechanically
placeholder-pruning a request copy. The write-side face is
``ScopedMessageHistory._run_cleanup_if_triggered``; both are thin callers of
the same interface and the same threshold function.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from modex_agent.core.message import MessageRole
from modex_agent.memory.budget import resolve_effective_budget
from modex_agent.memory.cleanup import CompactionSource, check_cleanup_trigger
from modex_agent.memory.context_governance import ContextGovernance
from modex_agent.memory.token_estimator import CharTokenEstimator, TokenEstimator

if TYPE_CHECKING:
    from modex_agent.core.agent import AgentContext
    from modex_agent.memory.core.system import ContextManagedMemorySystem
    from modex_agent.memory.scope import MemoryContext

logger = logging.getLogger(__name__)


class MemoryCompactionGovernance(ContextGovernance):
    """Read-side compaction trigger: over-budget payloads get really compacted.

    Unlike ``ContextBudgetGovernance`` (mechanical placeholder pruning on a
    request copy), this governance calls
    ``ContextManagedMemorySystem.compact_session`` — the persistent 5-phase
    pipeline with an LLM summary — and then rebuilds the message list from the
    refreshed history. It is best-effort: any failure is logged and the
    original messages pass through untouched (emergency tail-trimming in the
    LLM client remains the final fallback).
    """

    def __init__(
        self,
        memory_system: ContextManagedMemorySystem,
        memory_context_resolver: Callable[[AgentContext], MemoryContext],
        fallback_max_context_tokens: int | None,
        fallback_max_output_tokens: int = 0,
        token_estimator: TokenEstimator | None = None,
        trigger_ratio: float = 0.85,
    ) -> None:
        """Wire the governance to a memory system and its context resolver.

        Args:
            memory_system: the system whose ``compact_session`` is driven.
            memory_context_resolver: maps the turn's ``AgentContext`` to the
                ``MemoryContext``. MUST return the same instance ``load()``
                built for the session (memory stores are keyed by
                ``MemoryContext`` — a look-alike context would compact a
                different storage scope). ``MemorySystemContextManager.
                resolve_memory_context(session_id)`` is the intended source.
            fallback_max_context_tokens: static pool-level window used when
                the turn carries no model profile (None = no limit).
            fallback_max_output_tokens: static pool-level output reservation.
            token_estimator: estimator for the cheap pre-check; cached
                ``token_count`` values are preferred when present.
            trigger_ratio: compaction trigger ratio — the threshold shared
                with the append face. Semantics (and default) come from
                ``SessionConfig.max_token_ratio`` (PRD §4.3.2); thread the
                session config's value so the read-side pre-check and the
                write-side trigger stay on one line.
        """
        self._memory_system = memory_system
        self._memory_context_resolver = memory_context_resolver
        self._fallback_max_context_tokens = fallback_max_context_tokens
        self._fallback_max_output_tokens = fallback_max_output_tokens
        self._estimator: TokenEstimator = token_estimator or CharTokenEstimator()
        self._trigger_ratio = trigger_ratio

    async def apply(
        self,
        messages: list[dict[str, Any]],
        ctx: AgentContext,
    ) -> list[dict[str, Any]]:
        """Compact over-budget payloads; otherwise return the messages as-is.

        Shapes mirror ``LLMNode._build_messages`` exactly: the input is
        ``[system?] + ctx.to_messages()`` and — after a compaction — the
        output is rebuilt as ``[system?] + refreshed ctx.to_messages()``.
        Multimodal injection happens AFTER governance and is unaffected.
        """
        try:
            return await self._apply(messages, ctx)
        except Exception:
            # Best-effort layer: a compaction/refresh failure must never
            # break the LLM call — pass the original messages through and
            # let emergency compaction (llm_client) remain the fallback.
            logger.warning(
                "Memory compaction governance failed; passing messages through",
                exc_info=True,
            )
            return list(messages)

    async def _apply(
        self,
        messages: list[dict[str, Any]],
        ctx: AgentContext,
    ) -> list[dict[str, Any]]:
        # 1. Current-turn budget: ctx.runtime.model_info (None-safe) through
        #    the sole priority-chain resolver — never expanded here.
        model_info = ctx.runtime.model_info if ctx.runtime is not None else None
        budget = resolve_effective_budget(
            model_info,
            self._fallback_max_context_tokens,
            self._fallback_max_output_tokens,
        )
        if budget.max_context_tokens is None:
            return list(messages)

        # 2. Cheap pre-check on the assembled payload (system messages
        #    excluded, cached token_count preferred).
        reason = check_cleanup_trigger(
            messages,
            self._estimator,
            budget.max_context_tokens,
            self._trigger_ratio,
            budget.max_output_tokens,
        )
        if reason is None:
            return list(messages)

        # 3. Over pressure → persistent compaction with the current turn's
        #    resolved budget (limit + output reservation in one typed value).
        await self._memory_system.compact_session(
            self._memory_context_resolver(ctx),
            budget=budget,
            source=CompactionSource.PRE_LLM,
        )

        # 4. Rebuild from the refreshed history whenever compaction was
        #    INITIATED — regardless of the CleanupResult. PRD §4.3.2 steps
        #    4/5 ("triggered and pruned>0") are deliberately merged into
        #    "initiated ⇒ rebuild": the rebuild is idempotent, and it also
        #    covers the concurrent case where the write side compacted this
        #    session just before our call (revision conflict / pruned==0)
        #    and our history cache now holds a stale pre-compaction view.
        await ctx.history.refresh()
        rebuilt = await ctx.to_messages()
        if messages and messages[0].get("role") == str(MessageRole.SYSTEM):
            return [messages[0], *rebuilt]
        return rebuilt


__all__ = ["MemoryCompactionGovernance"]
