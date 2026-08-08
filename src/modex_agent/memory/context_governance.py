"""ContextGovernance implementations for pre-LLM context trimming.

Governance runs on a COPY of the model-visible message list; persisted history
is never modified.

Design (ADR-0009 + harness-improvement decisions):

The per-call governance chain is exactly two strategies:

1. ``ContextBudgetGovernance`` — token-window tool-result pruning.
   Replaces the former ``LossyContentCompactionGovernance``,
   ``MicrocompactGovernance``, and ``TokenBudgetGovernance`` (all removed).

2. ``ToolChainRepairGovernance`` — structural repair (backfill / orphan
   cleanup).  Does not modify content.

``EmergencyCompactionGovernance`` (in ``agents/react/error_recovery.py``)
remains as a reactive last-resort when the provider rejects a request for
context overflow.

Single-message overflow (tool results > 50 K chars) is handled at tool
execution time by ``ToolResultLimitInterceptor`` — governance does NOT
duplicate that work.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from modex_agent.core.governance import ContextGovernance
from modex_agent.core.types import MessageRole
from modex_agent.memory.token_estimator import CharTokenEstimator, TokenEstimator

if TYPE_CHECKING:
    from modex_agent.core.agent import AgentContext

logger = logging.getLogger(__name__)


# ── Metadata keys ───────────────────────────────────────────────────────────

META_CONTEXT_LOSSY = "meta_context_lossy"
META_ORIGINAL_CHARS = "meta_original_chars"
META_CONTEXT_REDUCTION = "meta_context_reduction"

# Fixed placeholder for cleared tool result content.
# Cache-friendly: every compacted tool result becomes identical regardless
# of the original content length.
_CLEARED_PLACEHOLDER = "[Old tool result content cleared]"

# Reduction type recorded in META_CONTEXT_REDUCTION for tool-result pruning.
_TOOL_RESULT_PRUNED = "tool_result_pruned"


# ── Composite ───────────────────────────────────────────────────────────────


class CompositeGovernance(ContextGovernance):
    """Run multiple governance strategies in sequence."""

    def __init__(self, strategies: list[ContextGovernance]) -> None:
        self._strategies = strategies

    async def apply(
        self,
        messages: list[dict[str, Any]],
        ctx: AgentContext,
    ) -> list[dict[str, Any]]:
        result = list(messages)
        for strategy in self._strategies:
            result = await strategy.apply(result, ctx)
        return result


# ── Tool-chain repair (structural, no content modification) ─────────────────


class ToolChainRepairGovernance(ContextGovernance):
    """Repair tool-call chain integrity in the model-visible message copy.

    Uses the session tool-chain sanitizer in MODEL_VISIBLE_CONTEXT mode to:
    - remove orphan tool results (no matching assistant tool_call), and
    - backfill dangling tool_calls: an assistant tool_call with no matching
      tool result is kept and a placeholder tool message (matched id) is
      synthesized so LLM providers never receive assistant messages with
      tool_calls but no matching tool results.

    Operates on a message copy only — persisted history is never modified.
    """

    async def apply(
        self,
        messages: list[dict[str, Any]],
        ctx: AgentContext,
    ) -> list[dict[str, Any]]:
        from modex_agent.memory.sanitizer import (
            DefaultSessionToolChainSanitizer,
            ToolChainSanitizationMode,
        )

        result = DefaultSessionToolChainSanitizer().sanitize(
            messages,
            mode=ToolChainSanitizationMode.MODEL_VISIBLE_CONTEXT,
        )
        return result.messages


# ── Context budget governance (token-window tool-result pruning) ────────────


def _resolve_message_tokens(
    message: dict[str, Any],
    estimator: TokenEstimator,
) -> int:
    """Return a message's token count: cached if valid, else recompute.

    Mirrors ``cleanup._resolve_message_tokens`` — the same estimator stamps
    ``token_count`` at append time, so the cache is authoritative.
    """
    cached = message.get("token_count")
    if isinstance(cached, int) and cached > 0:
        return cached
    return estimator.estimate_message(message)


class ContextBudgetGovernance(ContextGovernance):
    """Token-window tool-result pruning.

    Borrowed from opencode's ``prune`` design, adapted for ModexAgent's
    "governance doesn't persist" principle:

    - **Protect window**: the most recent ``protect_tokens`` of tool-result
      output are always kept verbatim.
    - **Min-gain gate**: if the replaceable tokens (those outside the
      protect window) total less than ``min_gain_tokens``, the entire
      pass is skipped — not worth the prefix change.
    - **One-shot replacement**: every tool result outside the window is
      replaced with the fixed ``_CLEARED_PLACEHOLDER`` in a single pass.
      No per-message re-estimation loop.

    Threshold (``governance_ratio``) is intentionally **below** the
    persistent-compaction threshold (``max_token_ratio``, default 0.85)
    so governance intervenes *before* a compact is triggered:

    ::

      0% ─────── governance_ratio (0.60) ──── max_token_ratio (0.85) ── 100%
      │  zero-mutation                        │  placeholder pruning   │  compact  │

    Within one compact cycle (30 % → 85 % → compact → 30 %) total tokens
    are monotonically increasing, so the protect window only grows: new
    tool results enter the window, old ones outside are deterministically
    replaced with the same constant placeholder.  The prefix is
    **expansion-only** — existing placeholders never change, each call may
    only add a few more at the head.

    **Idempotency**: ``meta_context_lossy`` guard runs *before* any content
    evaluation.  A message already marked lossy (defence-in-depth; should
    not exist in a fresh copy but guards against composite chains) is
    skipped entirely.  The placeholder (35 chars) can never trigger a
    length threshold because the guard short-circuits first.

    **No message dropping**: this governance never removes messages from
    the list — that is the responsibility of persistent compaction
    (``cleanup_session``) and emergency compaction
    (``EmergencyCompactionGovernance``).  Keeping the message count and
    ordering stable is what makes the prefix cache-friendly.
    """

    def __init__(
        self,
        max_context_tokens: int,
        token_estimator: TokenEstimator | None = None,
        governance_ratio: float = 0.60,
        protect_tokens: int = 40_000,
        min_gain_tokens: int = 20_000,
        keep_recent: int = 10,
        whitelist_tools: frozenset[str] | None = None,
    ) -> None:
        self._max_context_tokens = max_context_tokens
        self._estimator = token_estimator or CharTokenEstimator()
        self._threshold = int(max_context_tokens * governance_ratio)
        self._protect_tokens = protect_tokens
        self._min_gain = min_gain_tokens
        self._keep_recent = keep_recent
        self._whitelist = whitelist_tools or frozenset()

    async def apply(
        self,
        messages: list[dict[str, Any]],
        ctx: AgentContext,
    ) -> list[dict[str, Any]]:
        if not messages:
            return []

        # Step 0: estimate total tokens (cached token_count preferred).
        msg_tokens = [_resolve_message_tokens(m, self._estimator) for m in messages]
        total = sum(msg_tokens)

        # Step 1: zero-mutation path — under budget, touch nothing.
        if total <= self._threshold:
            return list(messages)

        # Step 2: token-window pruning.
        return self._prune_by_window(messages, msg_tokens)

    def _prune_by_window(
        self,
        messages: list[dict[str, Any]],
        msg_tokens: list[int],
    ) -> list[dict[str, Any]]:
        # 1. Collect compactable tool-result entries (index, tokens).
        tool_entries: list[tuple[int, int]] = []
        for i, m in enumerate(messages):
            if (
                m.get("role") == str(MessageRole.TOOL)
                and m.get("name") not in self._whitelist
            ):
                tool_entries.append((i, msg_tokens[i]))

        # Structural floor: keep at least ``keep_recent`` tool results.
        if len(tool_entries) <= self._keep_recent:
            return list(messages)

        # 2. Walk newest→oldest within eligible (beyond keep_recent floor),
        #    starting from the floor's token total so keep_recent and
        #    protect_tokens compose: floor guarantees a minimum count,
        #    protect_tokens caps the total retained tool-output size.
        floor_start = len(tool_entries) - self._keep_recent
        floor_tokens = sum(tok for _, tok in tool_entries[floor_start:])
        accumulated = floor_tokens
        window_start = 0  # default: nothing pruned (all fit)
        for j in range(floor_start - 1, -1, -1):
            accumulated += tool_entries[j][1]
            if accumulated > self._protect_tokens:
                window_start = j + 1
                break

        # 3. Candidates outside the window (oldest entries beyond floor).
        outside = tool_entries[:window_start]

        # 4. Min-gain gate: skip if the replaceable amount is too small.
        outside_tokens = sum(tok for _, tok in outside)
        if outside_tokens < self._min_gain:
            return list(messages)

        # 5. One-shot replacement of every tool result outside the window.
        result = [dict(m) for m in messages]
        for idx, _tok in outside:
            msg = result[idx]
            # Idempotency guard — defence-in-depth.
            if msg.get(META_CONTEXT_LOSSY, False):
                continue
            content = msg.get("content")
            if not isinstance(content, str) or not content:
                continue
            result[idx] = {
                **msg,
                "content": _CLEARED_PLACEHOLDER,
                META_CONTEXT_LOSSY: True,
                META_ORIGINAL_CHARS: len(content),
                META_CONTEXT_REDUCTION: _TOOL_RESULT_PRUNED,
            }

        return result
