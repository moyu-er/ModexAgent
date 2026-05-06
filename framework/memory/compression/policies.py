"""Compression policy ABCs and default implementations.

Phase 4 — these are the pluggable strategies that ``MemoryCompressionCoordinator``
assembles so that session-to-archive compaction is safe, testable, and
respects the two-phase optimistic-commit principle (no LLM call under lock).
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import Any

from framework.memory.compaction.boundary import BoundaryPolicy, ToolChainBoundaryPolicy
from framework.memory.compaction.policy import (
    ConservativeCompactionPolicy,
    MessageCompactionDecision,
    MessageCompactionPolicy,
)
from framework.memory.core.layers import ArchiveMemoryManager, SessionMemoryManager
from framework.memory.core.models import (
    ArchiveEntry,
    CompressionPlan,
    CompressionReason,
    CompressionResult,
    CompressionTrigger,
)
from framework.memory.core.scope import MemoryContext
from framework.memory.utils import normalize_memory_summary

logger = logging.getLogger(__name__)

# ── Trigger ──────────────────────────────────────────────────────────────────


class CompressionTriggerPolicy(ABC):
    """Decide whether the session layer needs compaction."""

    @abstractmethod
    async def should_compress(
        self,
        *,
        session: SessionMemoryManager,
        context: MemoryContext,
    ) -> CompressionTrigger | None: ...


class DefaultCompressionTriggerPolicy(CompressionTriggerPolicy):
    """Default trigger: check token count and message count against config."""

    def __init__(
        self,
        max_messages: int | None = 100,
        max_tokens: int | None = 8000,
    ) -> None:
        self._max_messages = max_messages
        self._max_tokens = max_tokens

    async def should_compress(
        self,
        *,
        session: SessionMemoryManager,
        context: MemoryContext,
    ) -> CompressionTrigger | None:
        all_msgs = await session.get_all_messages(context)
        all_msgs_count = len(all_msgs)

        if self._max_messages and all_msgs_count > self._max_messages:
            logger.info(
                "Compression triggered by MESSAGE_COUNT: msgs=%d > max_messages=%d",
                all_msgs_count, self._max_messages,
            )
            return CompressionTrigger(reason=CompressionReason.MESSAGE_COUNT)

        if self._max_tokens:
            estimated_tokens = self.estimate_tokens(all_msgs)
            if estimated_tokens > self._max_tokens:
                logger.info(
                    "Compression triggered by TOKEN_PRESSURE: tokens=%d > max_tokens=%d",
                    estimated_tokens, self._max_tokens,
                )
                return CompressionTrigger(
                    reason=CompressionReason.TOKEN_PRESSURE,
                    metadata={"estimated_tokens": estimated_tokens},
                )
        return None

    @staticmethod
    def estimate_tokens(messages: Sequence[Any]) -> int:
        try:
            from framework.memory.utils import estimate_token_count
            return estimate_token_count([m.to_dict() if hasattr(m, 'to_dict') else m for m in messages])
        except Exception:
            return sum(len(str(m)) // 4 for m in messages)


# ── Summary ─────────────────────────────────────────────────────────────────


class SummaryStrategy(ABC):
    """Generate a textual summary from a list of messages.

    Does NOT decide boundaries, mutate storage, or archive.
    """

    @abstractmethod
    async def summarize(
        self,
        messages: Sequence[dict[str, Any]],
        context: MemoryContext,
        reason: CompressionReason,
    ) -> str: ...


class HeuristicSummaryStrategy(SummaryStrategy):
    """Lightweight heuristic summary (no LLM, for tests)."""

    async def summarize(
        self,
        messages: Sequence[dict[str, Any]],
        context: MemoryContext,
        reason: CompressionReason,
    ) -> str:
        _ = context, reason
        parts = [m.get("content", "") for m in messages if m.get("role") == "user" and m.get("content")]
        if parts:
            return " | ".join(parts[:5])
        parts = [m.get("content", "") for m in messages if m.get("role") == "assistant" and m.get("content")]
        if parts:
            return " | ".join(parts[:3])
        return ""


# ── Error ────────────────────────────────────────────────────────────────────


class CompressionErrorPolicy(ABC):
    """Handle failures during compression without data loss."""

    @abstractmethod
    async def on_summary_failure(
        self, error: Exception, messages: list[dict[str, Any]], context: MemoryContext,
    ) -> str | None:
        """Return a fallback summary string, or None to abort compression."""
        ...

    @abstractmethod
    async def on_archive_failure(
        self, error: Exception, plan: CompressionPlan, context: MemoryContext,
    ) -> bool:
        """Return True if session mutation should still proceed despite archive failure."""
        ...

    @abstractmethod
    async def on_commit_conflict(
        self, plan: CompressionPlan, context: MemoryContext,
    ) -> bool:
        """Return True to retry immediately, False to skip."""
        ...


class DefaultCompressionErrorPolicy(CompressionErrorPolicy):
    """Conservative defaults: never lose data, never retry in hot path."""

    async def on_summary_failure(
        self, error: Exception, messages: list[dict[str, Any]], context: MemoryContext,
    ) -> str | None:
        _ = context
        logger.warning("LLM summary failed — using raw fallback: %s", error)
        lines = [
            f"{m.get('role', '?')}: {str(m.get('content', ''))[:200]}"
            for m in messages if m.get("content")
        ]
        return "\n".join(lines) if lines else None

    async def on_archive_failure(
        self, error: Exception, plan: CompressionPlan, context: MemoryContext,
    ) -> bool:
        _ = context
        logger.warning("Archive append failed — preserving session: %s", error)
        return False  # Never mutate session if archive write failed

    async def on_commit_conflict(
        self, plan: CompressionPlan, context: MemoryContext,
    ) -> bool:
        _ = context
        logger.debug("Compression commit conflict for %s — skipping", context.session_id)
        return False  # Don't retry; next add_messages will trigger fresh compression


# ── Commit ───────────────────────────────────────────────────────────────────


class CommitPolicy(ABC):
    """Two-phase commit for session → archive compaction.

    Phase 3 of the compression protocol: re-acquire lock, check revision,
    write archive, mutate session.
    """

    @abstractmethod
    async def commit(
        self,
        *,
        plan: CompressionPlan,
        session: SessionMemoryManager,
        archive: ArchiveMemoryManager,
        context: MemoryContext,
        error_policy: CompressionErrorPolicy,
    ) -> CompressionResult: ...


class DefaultCommitPolicy(CommitPolicy):
    """Default two-phase optimistic commit.

    1. Re-read revision from session storage.
    2. If revision changed → skip (concurrent modification).
    3. Write archive entry.
    4. Replace session messages with keep_messages.
    """

    async def commit(
        self,
        *,
        plan: CompressionPlan,
        session: SessionMemoryManager,
        archive: ArchiveMemoryManager,
        context: MemoryContext,
        error_policy: CompressionErrorPolicy,
    ) -> CompressionResult:
        current_revision = await session.get_revision(context)
        if (
            current_revision.version != plan.expected_revision.version
            or current_revision.message_count != plan.expected_revision.message_count
        ):
            await error_policy.on_commit_conflict(plan, context)
            return CompressionResult(committed=False, retryable=False, reason="revision_changed")

        # Archive first — skip empty or placeholder summaries
        normalized_summary = normalize_memory_summary(plan.summary)
        wrote_archive = False
        if normalized_summary is not None:
            try:
                entry = ArchiveEntry(
                    summary=normalized_summary,
                    metadata={"reason": str(plan.trigger.reason), "source": "compression"},
                )
                await archive.append(context, entry)
                wrote_archive = True
            except Exception as exc:
                proceed = await error_policy.on_archive_failure(exc, plan, context)
                if not proceed:
                    return CompressionResult(committed=False, retryable=True, reason="archive_failed")
                # Fall through to still mutate session (error policy said proceed)

        extra_state: dict[str, Any] = {}
        if wrote_archive and normalized_summary is not None:
            extra_state[".compression_summary"] = normalized_summary

        revision = await session.replace_messages_if_revision(
            context,
            plan.keep_messages,
            plan.expected_revision,
            extra_state,
        )
        if revision is None:
            await error_policy.on_commit_conflict(plan, context)
            return CompressionResult(committed=False, retryable=False, reason="revision_changed")

        if not wrote_archive:
            return CompressionResult(committed=True, reason="nothing_to_archive")

        return CompressionResult(committed=True)


# ── Coordinator ──────────────────────────────────────────────────────────────


class MemoryCompressionCoordinator(ABC):
    """Orchestrate session → archive compaction with pluggable policies.

    This is the single entry point for compressing session messages.  It
    does NOT hold any storage lock during LLM calls — the two-phase
    protocol ensures safety.
    """

    @abstractmethod
    async def maybe_compress(
        self,
        *,
        session: SessionMemoryManager,
        archive: ArchiveMemoryManager,
        context: MemoryContext,
    ) -> CompressionResult: ...


class DefaultMemoryCompressionCoordinator(MemoryCompressionCoordinator):
    """Default coordinator: trigger → plan → summary → commit.

    Uses ``MessageCompactionPolicy`` to classify each visible message before
    boundary selection, so tool-call chains and KEEP_RAW messages are protected
    and DROP_FROM_SUMMARY messages are excluded from summary input.
    """

    def __init__(
        self,
        *,
        trigger: CompressionTriggerPolicy | None = None,
        boundary: BoundaryPolicy | None = None,
        summary: SummaryStrategy | None = None,
        commit: CommitPolicy | None = None,
        error_policy: CompressionErrorPolicy | None = None,
        max_messages: int | None = 100,
        max_tokens: int | None = 8000,
        compaction: MessageCompactionPolicy | None = None,
        keep_ratio_for_messages: float = 0.5,
        keep_ratio_for_token: float = 0.5,
    ) -> None:
        self._max_messages = max_messages
        self._trigger = trigger or DefaultCompressionTriggerPolicy(
            max_messages=max_messages, max_tokens=max_tokens,
        )
        self._boundary = boundary or ToolChainBoundaryPolicy()
        self._summary = summary or HeuristicSummaryStrategy()
        self._commit = commit or DefaultCommitPolicy()
        self._error = error_policy or DefaultCompressionErrorPolicy()
        self._compaction = compaction or ConservativeCompactionPolicy()
        self._keep_ratio_for_messages = max(0.2, min(keep_ratio_for_messages, 0.9))
        self._keep_ratio_for_token = max(0.2, min(keep_ratio_for_token, 0.9))

    async def maybe_compress(
        self,
        *,
        session: SessionMemoryManager,
        archive: ArchiveMemoryManager,
        context: MemoryContext,
    ) -> CompressionResult:
        # Phase 1: Trigger check
        trigger = await self._trigger.should_compress(session=session, context=context)
        if trigger is None:
            return CompressionResult(committed=True, reason="not_needed")

        # Phase 2: Read ALL stored messages and classify every one.
        all_msgs = [m.to_dict() for m in await session.get_all_messages(context)]
        if not all_msgs:
            return CompressionResult(committed=True, reason="empty")

        decisions = self._compaction.decide_all(all_msgs, context, str(trigger.reason))

        # Phase 3: Compute boundary and summary (LLM may be called — NO lock)
        if trigger.reason == CompressionReason.TOKEN_PRESSURE:
            all_tokens = self._trigger.estimate_tokens(all_msgs)
            target_tokens = int(all_tokens * self._keep_ratio_for_token)
            keep_target = self._compute_keep_by_token_budget(all_msgs, target_tokens)
            logger.info(
                "Compression by TOKEN_PRESSURE: all_tokens=%d, target_tokens=%d, keep_target=%d",
                all_tokens, target_tokens, keep_target,
            )
        elif trigger.reason == CompressionReason.MESSAGE_COUNT:
            trigger_max_messages = getattr(self._trigger, "_max_messages", self._max_messages)
            keep_target = max(1, int((trigger_max_messages or 100) * self._keep_ratio_for_messages))
            logger.info(
                "Compression by MESSAGE_COUNT: keep_target=%d (max_messages=%s * keep_ratio_for_messages=%.2f)",
                keep_target, trigger_max_messages or 100, self._keep_ratio_for_messages,
            )
        else:
            trigger_max_messages = getattr(self._trigger, "_max_messages", self._max_messages)
            keep_target = max(1, int((trigger_max_messages or 100) * self._keep_ratio_for_messages))

        prune_count = max(0, len(all_msgs) - keep_target)
        if prune_count <= 0:
            return CompressionResult(committed=True, reason="within_budget")

        boundary_idx = self._find_boundary(all_msgs, decisions, prune_count)
        if boundary_idx <= 0:
            logger.info("No safe boundary found for compression (prune_count=%d)", prune_count)
            return CompressionResult(committed=True, reason="no_safe_boundary")

        logger.info(
            "Compression plan: total_msgs=%d, prune_count=%d, keep_count=%d, boundary_idx=%d",
            len(all_msgs), prune_count, len(all_msgs) - boundary_idx, boundary_idx,
        )

        # Pruned prefix: classify by decision
        pruned = all_msgs[:boundary_idx]
        keep = all_msgs[boundary_idx:]
        pruned_decisions = decisions[:boundary_idx]

        # Messages marked SUMMARIZE go to the LLM/heuristic summary
        summarized = [
            m for m, d in zip(pruned, pruned_decisions, strict=True)
            if d == MessageCompactionDecision.SUMMARIZE
        ]
        # Messages marked DROP_FROM_SUMMARY are pruned but omitted from summary
        dropped = [
            m for m, d in zip(pruned, pruned_decisions, strict=True)
            if d == MessageCompactionDecision.DROP_FROM_SUMMARY
        ]
        # ARCHIVE_RAW messages are recorded but not summarized
        archive_raw = [
            m for m, d in zip(pruned, pruned_decisions, strict=True)
            if d == MessageCompactionDecision.ARCHIVE_RAW
        ]

        summary = ""
        if summarized:
            summary = await self._summarize_with_fallback(summarized, context, trigger.reason)

        # Phase 4: Build plan
        revision = await session.get_revision(context)
        plan = CompressionPlan(
            trigger=trigger,
            expected_revision=revision,
            expected_cursor=None,
            keep_messages=keep,
            summarize_messages=summarized,
            archive_raw_messages=archive_raw,
            drop_messages=dropped,
            summary=summary,
        )

        # Phase 5: Commit (caller ensures lock is held)
        return await self._commit.commit(
            plan=plan,
            session=session,
            archive=archive,
            context=context,
            error_policy=self._error,
        )

    def _compute_keep_by_token_budget(
        self,
        messages: list[dict[str, Any]],
        target_tokens: int,
    ) -> int:
        total = len(messages)
        accumulated = 0
        for i in range(total - 1, -1, -1):
            msg_tokens = len(str(messages[i])) // 4
            accumulated += msg_tokens
            if accumulated >= target_tokens:
                return total - i
        return total

    def _find_boundary(
        self,
        messages: list[dict[str, Any]],
        decisions: list[MessageCompactionDecision],
        target_prune: int,
    ) -> int:
        """Find a safe truncation boundary that respects compaction decisions."""
        if self._boundary is not None:
            return int(self._boundary.find_prune_boundary(messages, decisions, target_prune))
        # Simple heuristic: cut at the last user message before target_prune
        boundary = target_prune
        for i in range(target_prune, min(target_prune + 20, len(messages))):
            if messages[i].get("role") == "user":
                boundary = i
                break
        return boundary

    async def _summarize_with_fallback(
        self,
        messages: list[dict[str, Any]],
        context: MemoryContext,
        reason: CompressionReason,
    ) -> str:
        try:
            return await self._summary.summarize(messages, context, reason)
        except Exception as exc:
            fallback = await self._error.on_summary_failure(exc, messages, context)
            return fallback or ""
