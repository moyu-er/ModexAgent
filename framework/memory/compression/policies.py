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
from framework.memory.core.layers import ArchiveMemoryManager, SessionMemoryManager
from framework.memory.core.models import (
    ArchiveEntry,
    CompressionPlan,
    CompressionReason,
    CompressionResult,
    CompressionTrigger,
)
from framework.memory.core.scope import MemoryContext

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

    COOLDOWN_KEY = ".last_compression"

    def __init__(
        self,
        max_messages: int | None = 100,
        max_tokens: int | None = 8000,
        cooldown_messages: int = 5,
    ) -> None:
        self._max_messages = max_messages
        self._max_tokens = max_tokens
        self._cooldown = cooldown_messages

    async def should_compress(
        self,
        *,
        session: SessionMemoryManager,
        context: MemoryContext,
    ) -> CompressionTrigger | None:
        all_msgs = await session.get_all_messages(context)
        visible = await session.get_visible_messages(context)

        # Cooldown: don't re-trigger for small deltas (persisted to session storage)
        last_raw = await session.get_state(context, self.COOLDOWN_KEY)
        last = int(last_raw) if isinstance(last_raw, int | float | str) else 0
        if len(visible) - last < self._cooldown:
            return None

        if self._max_messages and len(visible) > self._max_messages:
            return CompressionTrigger(reason=CompressionReason.MESSAGE_COUNT)
        if self._max_tokens and self._estimate_tokens(visible) > self._max_tokens:
            return CompressionTrigger(reason=CompressionReason.TOKEN_PRESSURE)
        if len(all_msgs) > len(visible):
            return CompressionTrigger(reason=CompressionReason.TOKEN_PRESSURE, score=0.5)
        return None

    @staticmethod
    def _estimate_tokens(messages: Sequence[Any]) -> int:
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
        _EMPTY_SUMMARIES = frozenset({"", "(no conversation content)", "(no summary)", "(nothing)"})
        if (plan.summary or "").strip() in _EMPTY_SUMMARIES:
            return CompressionResult(committed=True, reason="empty_summary_skipped")
        try:
            entry = ArchiveEntry(
                summary=plan.summary or "",
                metadata={"reason": str(plan.trigger.reason), "source": "compression"},
            )
            await archive.append(context, entry)
        except Exception as exc:
            proceed = await error_policy.on_archive_failure(exc, plan, context)
            if not proceed:
                return CompressionResult(committed=False, retryable=True, reason="archive_failed")
            # Fall through to still mutate session (error policy said proceed)

        revision = await session.replace_messages_if_revision(
            context,
            plan.keep_messages,
            plan.expected_revision,
            {
                ".compression_summary": plan.summary or "",
                ".last_compression": len(plan.keep_messages),
            },
        )
        if revision is None:
            await error_policy.on_commit_conflict(plan, context)
            return CompressionResult(committed=False, retryable=False, reason="revision_changed")

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
    """Default coordinator: trigger → plan → summary → commit."""

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
    ) -> None:
        self._max_messages = max_messages
        self._trigger = trigger or DefaultCompressionTriggerPolicy(
            max_messages=max_messages, max_tokens=max_tokens,
        )
        self._boundary = boundary or ToolChainBoundaryPolicy()
        self._summary = summary or HeuristicSummaryStrategy()
        self._commit = commit or DefaultCommitPolicy()
        self._error = error_policy or DefaultCompressionErrorPolicy()

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

        # Phase 2: Read visible snapshot for boundary and summary selection.
        visible = [m.to_dict() for m in await session.get_visible_messages(context)]
        if not visible:
            return CompressionResult(committed=True, reason="empty")

        # Phase 3: Compute boundary and summary (LLM may be called — NO lock)
        # Derive prune threshold from trigger policy to avoid divergence
        trigger_max_messages = getattr(self._trigger, "_max_messages", self._max_messages)
        prune_count = max(0, len(visible) - (trigger_max_messages or 100))
        if prune_count <= 0:
            return CompressionResult(committed=True, reason="within_budget")

        boundary_idx = self._find_boundary(visible, prune_count)
        if boundary_idx <= 0:
            return CompressionResult(committed=True, reason="no_safe_boundary")

        summarized = visible[:boundary_idx]
        keep = visible[boundary_idx:]

        summary = await self._summarize_with_fallback(summarized, context, trigger.reason)

        # Phase 4: Build plan
        revision = await session.get_revision(context)
        plan = CompressionPlan(
            trigger=trigger,
            expected_revision=revision,
            expected_cursor=None,
            keep_messages=keep,
            summarize_messages=summarized,
            archive_raw_messages=[],
            drop_messages=[],
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

    def _find_boundary(self, messages: list[dict[str, Any]], target_prune: int) -> int:
        """Find a safe truncation boundary (default: user-turn boundary)."""
        if self._boundary is not None:
            return int(self._boundary.find_prune_boundary(messages, [], target_prune))
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
