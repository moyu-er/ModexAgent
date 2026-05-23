"""Compression policy ABCs and default implementations.

Phase 4 — these are the pluggable strategies that ``MemoryCompressionCoordinator``
assembles so that session-to-archive compaction is safe, testable, and
respects the two-phase optimistic-commit principle (no LLM call under lock).
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections import Counter
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from framework.memory.archive_models import ArchiveChannel
from framework.memory.compaction.boundary import BoundaryPolicy, ToolChainBoundaryPolicy
from framework.memory.compaction.policy import (
    ConservativeCompactionPolicy,
    MessageCompactionDecision,
    MessageCompactionPolicy,
)
from framework.memory.compression.planner import (
    CompressionBudget,
    CompressionKeepPlanner,
    PriorityCompressionKeepPlanner,
)
from framework.memory.compression.tool_chain_sanitizer import (
    DefaultSessionToolChainSanitizer,
    SessionToolChainSanitizer,
    ToolChainSanitizationMode,
)
from framework.memory.core.layers import (
    ArchiveMemoryManager,
    PendingPrunedInputMemoryManager,
    SessionMemoryManager,
)
from framework.memory.core.models import (
    CompressionPlan,
    CompressionReason,
    CompressionResult,
    CompressionResultReason,
    CompressionTrigger,
)
from framework.memory.core.scope import MemoryContext
from framework.memory.pending import DefaultPendingPrunedInputExtractor, PendingPrunedInputExtractor
from framework.memory.retention import DefaultMessageRetentionPolicy, MessageRetentionPolicy
from framework.memory.utils import normalize_memory_summary

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from framework.memory.archive_generation import ArchiveGenerationStrategy
    from framework.memory.archive_models import ArchiveGenerationResult

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

    @staticmethod
    def estimate_tokens(messages: Sequence[Any]) -> int:
        """Estimate token count for a list of messages."""
        try:
            from framework.memory.utils import estimate_token_count
            return estimate_token_count([m.to_dict() if hasattr(m, "to_dict") else m for m in messages])
        except Exception:
            return sum(len(str(m)) // 4 for m in messages)


class DefaultCompressionTriggerPolicy(CompressionTriggerPolicy):
    """Default trigger: check token count and message count against config."""

    def __init__(
        self,
        max_messages: int | None = 100,
        max_tokens: int | None = 8000,
    ) -> None:
        self._max_messages = max_messages
        self._max_tokens = max_tokens

    @property
    def max_messages(self) -> int | None:
        return self._max_messages

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
        archive: ArchiveMemoryManager | None,
        pending: PendingPrunedInputMemoryManager | None = None,
        context: MemoryContext,
        error_policy: CompressionErrorPolicy,
    ) -> CompressionResult: ...


class DefaultCommitPolicy(CommitPolicy):
    """Default two-phase optimistic commit.

    1. Re-read revision from session storage.
    2. If revision changed → skip (concurrent modification).
    3. Write archive entry when an archive layer is configured.
    4. Replace session messages with keep_messages.

    archive=None is the session-only mode used by subagent memory:
    the same trigger, planner, and hard keep constraints still apply, but
    commit only replaces the session and does not create an archive entry.
    """

    async def commit(
        self,
        *,
        plan: CompressionPlan,
        session: SessionMemoryManager,
        archive: ArchiveMemoryManager | None,
        pending: PendingPrunedInputMemoryManager | None = None,
        context: MemoryContext,
        error_policy: CompressionErrorPolicy,
    ) -> CompressionResult:
        current_revision = await session.get_revision(context)
        if (
            current_revision.version != plan.expected_revision.version
            or current_revision.message_count != plan.expected_revision.message_count
        ):
            await error_policy.on_commit_conflict(plan, context)
            return CompressionResult(committed=False, retryable=False, reason=CompressionResultReason.REVISION_CHANGED)

        # Archive first — skip empty or placeholder summaries
        wrote_archive = False
        if archive is not None:
            if not self._has_valid_archive_writes(plan.archive_generation_result):
                return CompressionResult(
                    committed=False,
                    retryable=False,
                    reason=CompressionResultReason.NOTHING_TO_ARCHIVE,
                )
            try:
                assert plan.archive_generation_result is not None
                result = await archive.append_bundle(context, plan.archive_generation_result.writes)
                wrote_archive = bool(result.written_channels)
            except Exception as exc:
                proceed = await error_policy.on_archive_failure(exc, plan, context)
                if not proceed:
                    return CompressionResult(committed=False, retryable=True, reason=CompressionResultReason.ARCHIVE_FAILED)
                # Fall through to still mutate session (error policy said proceed)
        pending_snapshot: list[Any] | None = None
        if pending is not None and plan.pending_pruned_input_entries:
            try:
                pending_snapshot = await pending.get_entries(context)
                await pending.append_entries(context, plan.pending_pruned_input_entries)
            except Exception:
                logger.warning("Pending input persistence failed; preserving session", exc_info=True)
                return CompressionResult(
                    committed=False,
                    retryable=True,
                    reason=CompressionResultReason.PENDING_FAILED,
                )

        revision = await session.replace_messages_if_revision(
            context,
            plan.keep_messages,
            plan.expected_revision,
            {},
            idle_threshold_seconds=plan.idle_threshold_seconds,
        )
        if revision is None:
            if pending is not None and pending_snapshot is not None:
                try:
                    await pending.replace_entries(context, pending_snapshot)
                except Exception:
                    logger.warning(
                        "Failed to restore pending input snapshot after commit conflict",
                        exc_info=True,
                    )
            await error_policy.on_commit_conflict(plan, context)
            return CompressionResult(committed=False, retryable=False, reason=CompressionResultReason.REVISION_CHANGED)

        if not wrote_archive:
            return CompressionResult(committed=True, reason=CompressionResultReason.NOTHING_TO_ARCHIVE)

        return CompressionResult(committed=True)

    @staticmethod
    def _has_valid_archive_writes(result: ArchiveGenerationResult | None) -> bool:
        """Check whether archive generation produced at least one valid write.

        Previously required BOTH CONTEXT and KNOWLEDGE channels, which was
        overly conservative — a single valid channel is sufficient to create
        an archive entry.  Empty writes or None result still aborts.
        """
        if result is None:
            return False
        return len(result.writes) > 0


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
        archive: ArchiveMemoryManager | None,
        pending: PendingPrunedInputMemoryManager | None = None,
        context: MemoryContext,
        idle_threshold_seconds: float | None = None,
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
        retention: MessageRetentionPolicy | None = None,
        keep_planner: CompressionKeepPlanner | None = None,
        pending_extractor: PendingPrunedInputExtractor | None = None,
        tool_chain_sanitizer: SessionToolChainSanitizer | None = None,
        archive_generation: ArchiveGenerationStrategy | None = None,
    ) -> None:
        self._max_messages = max_messages
        self._trigger = trigger or DefaultCompressionTriggerPolicy(
            max_messages=max_messages, max_tokens=max_tokens,
        )
        self._boundary = boundary or ToolChainBoundaryPolicy()
        self._summary = summary
        self._commit = commit or DefaultCommitPolicy()
        self._error = error_policy or DefaultCompressionErrorPolicy()
        self._compaction = compaction or ConservativeCompactionPolicy()
        self._keep_ratio_for_messages = max(0.2, min(keep_ratio_for_messages, 0.9))
        self._keep_ratio_for_token = max(0.2, min(keep_ratio_for_token, 0.9))
        self._retention = retention or DefaultMessageRetentionPolicy()
        self._keep_planner = keep_planner or PriorityCompressionKeepPlanner()
        self._pending_extractor = pending_extractor or DefaultPendingPrunedInputExtractor()
        self._tool_chain_sanitizer = tool_chain_sanitizer or DefaultSessionToolChainSanitizer()
        self._archive_generation = archive_generation

    async def maybe_compress(
        self,
        *,
        session: SessionMemoryManager,
        archive: ArchiveMemoryManager | None,
        pending: PendingPrunedInputMemoryManager | None = None,
        context: MemoryContext,
        idle_threshold_seconds: float | None = None,
    ) -> CompressionResult:
        # Phase 1: Trigger check
        trigger = await self._trigger.should_compress(session=session, context=context)
        if trigger is None:
            return CompressionResult(committed=True, reason=CompressionResultReason.NOT_NEEDED)

        # Phase 2: Read ALL stored messages and sanitize tool-chain structure.
        all_msgs = [m.to_dict() for m in await session.get_all_messages(context)]
        if not all_msgs:
            return CompressionResult(committed=True, reason=CompressionResultReason.EMPTY)

        try:
            sanitization = self._tool_chain_sanitizer.sanitize(
                all_msgs,
                mode=ToolChainSanitizationMode.PERSISTENT_SESSION,
            )
        except Exception:
            logger.warning("Session tool-chain sanitization failed", exc_info=True)
            return CompressionResult(committed=True, reason=CompressionResultReason.NO_SAFE_BOUNDARY)

        if sanitization.removed_messages:
            counts = Counter(issue.reason for issue in sanitization.issues)
            logger.info(
                "Session tool-chain sanitizer removed invalid messages: "
                "session=%s removed=%d reasons=%s open_tail=%s",
                context.session_id,
                len(sanitization.removed_messages),
                {str(reason): count for reason, count in counts.items()},
                sanitization.has_open_tail,
            )

        sanitized_msgs = sanitization.messages
        if not sanitized_msgs:
            revision = await session.get_revision(context)
            plan = CompressionPlan(
                trigger=trigger,
                expected_revision=revision,
                expected_cursor=None,
                keep_messages=[],
                summarize_messages=[],
                archive_raw_messages=[],
                drop_messages=[],
                summary="",
                drop_without_archive_messages=sanitization.removed_messages,
                sanitization_issues=sanitization.issues,
                has_open_tail=sanitization.has_open_tail,
                idle_threshold_seconds=idle_threshold_seconds,
            )
            return await self._commit.commit(
                plan=plan,
                session=session,
                archive=None,
                pending=pending,
                context=context,
                error_policy=self._error,
            )

        all_msgs = sanitized_msgs

        decisions = self._compaction.decide_all(all_msgs, context, str(trigger.reason))

        retention = [
            self._retention.decide(m, index=i, messages=all_msgs, context=context)
            for i, m in enumerate(all_msgs)
        ]

        # Phase 3: Compute boundary and summary (LLM may be called — NO lock)
        if trigger.reason == CompressionReason.TOKEN_PRESSURE:
            all_tokens = self._trigger.estimate_tokens(all_msgs)
            max_keep_tokens = max(1, int(all_tokens * self._keep_ratio_for_token))
            budget = CompressionBudget(
                reason=trigger.reason,
                max_keep_messages=None,
                max_keep_tokens=max_keep_tokens,
            )
        else:
            trigger_max_messages = getattr(self._trigger, "max_messages", self._max_messages)
            max_keep_messages = max(
                1,
                int((trigger_max_messages or len(all_msgs)) * self._keep_ratio_for_messages),
            )
            budget = CompressionBudget(
                reason=trigger.reason,
                max_keep_messages=max_keep_messages,
                max_keep_tokens=None,
            )

        keep_plan = self._keep_planner.plan_keep_set(all_msgs, decisions, retention, budget)
        if not keep_plan.keep_messages or not keep_plan.within_budget:
            logger.info("No safe priority keep plan found: reason=%s", keep_plan.reason)
            return CompressionResult(committed=True, reason=CompressionResultReason.NO_SAFE_BOUNDARY)

        boundary_idx = keep_plan.keep_start_index
        pruned = keep_plan.pruned_messages
        keep = keep_plan.keep_messages
        pruned_indices_set = set(keep_plan.pruned_indices)

        prune_count = len(pruned)
        logger.info(
            "Compression plan: total_msgs=%d, prune_count=%d, keep_count=%d, boundary_idx=%d",
            len(all_msgs), prune_count, len(keep), boundary_idx,
        )
        pruned_decisions = [decisions[idx] for idx in keep_plan.pruned_indices]

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
        archive_generation_result = None
        if archive is not None and summarized and self._archive_generation is not None:
            archive_generation_result = await self._archive_generation.generate(
                summarized,
                context,
                trigger.reason,
            )
        elif archive is None and summarized:
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
            archive_generation_result=archive_generation_result,
            pending_pruned_input_entries=self._pending_extractor.extract(
                all_msgs,
                pruned_indices_set,
            ),
            drop_without_archive_messages=sanitization.removed_messages,
            sanitization_issues=sanitization.issues,
            has_open_tail=sanitization.has_open_tail,
            idle_threshold_seconds=idle_threshold_seconds,
        )

        # Phase 5: Commit (caller ensures lock is held)
        return await self._commit.commit(
            plan=plan,
            session=session,
            archive=archive,
            pending=pending,
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
        if self._summary is None:
            return ""
        try:
            return await self._summary.summarize(messages, context, reason)
        except Exception as exc:
            fallback = await self._error.on_summary_failure(exc, messages, context)
            normalized = normalize_memory_summary(fallback)
            return normalized or ""
