"""MemoryCompactionPipeline: unified orchestration for short-term compaction.

Token-pressure compression and idle AutoCompact both enter this pipeline,
ensuring they use the same message policy, boundary rules, summary strategy,
and archive format.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from framework.memory.archive import ArchiveStrategy

logger = logging.getLogger(__name__)
from framework.memory.compaction.boundary import BoundaryPolicy, ToolChainBoundaryPolicy
from framework.memory.compaction.policy import (
    ConservativeCompactionPolicy,
    MessageCompactionDecision,
    MessageCompactionPolicy,
)
from framework.memory.core.compression import CompressionResult
from framework.memory.core.message import ChatMessage
from framework.memory.core.scope import MemoryContext


class SummaryStrategy(ABC):
    """Generate a textual summary from a list of messages.

    This replaces the monolithic ``CompressionStrategy.compress()`` with a
    focused "summarize only" responsibility. Boundary selection and message
    filtering are handled by the pipeline itself.
    """

    @abstractmethod
    async def summarize(
        self,
        messages: Sequence[ChatMessage | dict[str, Any]],
        context: MemoryContext,
        reason: str,
    ) -> str:
        """Return a summary string (may be empty)."""
        raise NotImplementedError


class HeuristicSummaryStrategy(SummaryStrategy):
    """Lightweight heuristic summary for environments without an LLM."""

    async def summarize(
        self,
        messages: Sequence[ChatMessage | dict[str, Any]],
        context: MemoryContext,
        reason: str,
    ) -> str:
        _ = context, reason
        parts: list[str] = []
        for msg in messages:
            if isinstance(msg, ChatMessage):
                role = msg.role
                raw_content = msg.content
                content = raw_content if isinstance(raw_content, str) else ""
            else:
                role = msg.get("role", "unknown")
                raw_content = msg.get("content")
                content = raw_content if isinstance(raw_content, str) else ""
            if role == "user" and content:
                parts.append(content)
        if parts:
            return " | ".join(parts[:5])
        return ""


class ConsolidatorSummaryStrategy(SummaryStrategy):
    """Adapter that turns a ``CompressionStrategy`` (e.g. Consolidator)
    into a pure ``SummaryStrategy``.

    The adapted consolidator is only asked for the summary text; boundary
    selection and message filtering are ignored because the pipeline handles
    them separately.
    """

    def __init__(self, consolidator: Any) -> None:
        self._consolidator = consolidator

    async def summarize(
        self,
        messages: Sequence[ChatMessage | dict[str, Any]],
        context: MemoryContext,
        reason: str,
    ) -> str:
        from framework.memory.core.compression import CompressionContext

        _ = context, reason
        # The consolidator may expect ChatMessage instances
        chat_msgs = [ChatMessage.coerce(m) for m in messages]
        ctx = CompressionContext(
            token_count=0,
            target_token_count=None,
        )
        result = await self._consolidator.compress(chat_msgs, ctx)
        return result.summary or ""


@dataclass
class MemoryCompactionResult:
    """Result of a compaction pipeline run."""

    remaining_messages: list[dict[str, Any]] = field(default_factory=list)
    """Messages to retain in short-term memory."""

    pruned_messages: list[dict[str, Any]] = field(default_factory=list)
    """All messages that were pruned (regardless of disposition)."""

    summarized_messages: list[dict[str, Any]] = field(default_factory=list)
    """Subset of pruned messages that were fed to the summary strategy."""

    raw_archived_messages: list[dict[str, Any]] = field(default_factory=list)
    """Subset of pruned messages archived in raw form."""

    dropped_messages: list[dict[str, Any]] = field(default_factory=list)
    """Subset of pruned messages dropped without archiving."""

    summary: str | None = None
    """Generated summary text (may be empty or None)."""

    archived: bool = False
    """True if an archive write was attempted (regardless of success)."""


class MemoryCompactionPipeline:
    """Unified compaction pipeline.

    Execution order:
    1. MessageCompactionPolicy classifies every message.
    2. BoundaryPolicy finds a safe prune boundary.
    3. Pruned messages are split into summarize / raw-archive / drop buckets.
    4. SummaryStrategy generates a summary from the *summarize* bucket.
    5. ArchiveStrategy writes to history (if a history_manager is provided).
    6. On successful archive, short-term memory may be overwritten with
       ``remaining_messages`` by the caller.
    """

    def __init__(
        self,
        policy: MessageCompactionPolicy | None = None,
        boundary_policy: BoundaryPolicy | None = None,
        summary_strategy: SummaryStrategy | None = None,
        archive_strategy: ArchiveStrategy | None = None,
        history_manager: Any | None = None,
    ) -> None:
        self._policy = policy or ConservativeCompactionPolicy()
        self._boundary = boundary_policy or ToolChainBoundaryPolicy()
        self._summary_strategy = summary_strategy or HeuristicSummaryStrategy()
        self._archive_strategy = archive_strategy
        self._history_manager = history_manager

    async def run(
        self,
        context: MemoryContext,
        messages: Sequence[ChatMessage | dict[str, Any]],
        reason: str,
        keep_recent_messages: int | None = None,
    ) -> MemoryCompactionResult:
        """Run the compaction pipeline.

        Args:
            context: Memory context for scope isolation.
            messages: Full short-term message list.
            reason: Trigger reason (``"token_pressure"``, ``"idle_compact"``, …).
            keep_recent_messages: If provided, at least this many tail messages
                are retained. The boundary policy may force more to be kept.

        Returns:
            MemoryCompactionResult with remaining and pruned messages.
        """
        msg_list = list(messages)
        if not msg_list:
            return MemoryCompactionResult(remaining_messages=[])

        # 1. Classify every message
        decisions = self._policy.decide_all(msg_list, context, reason)

        # 2. Compute target prune count
        if keep_recent_messages is not None:
            target_prune = max(0, len(msg_list) - keep_recent_messages)
        else:
            target_prune = 0

        if target_prune <= 0:
            return MemoryCompactionResult(remaining_messages=[_to_dict(m) for m in msg_list])

        # 3. Find safe boundary
        boundary = self._boundary.find_prune_boundary(msg_list, decisions, target_prune)

        pruned_raw = msg_list[:boundary]
        remaining_raw = msg_list[boundary:]

        # 4. Split pruned messages by disposition
        summarized: list[dict[str, Any]] = []
        raw_archived: list[dict[str, Any]] = []
        dropped: list[dict[str, Any]] = []
        for idx, msg in enumerate(pruned_raw):
            decision = decisions[idx]
            msg_dict = _to_dict(msg)
            if decision == MessageCompactionDecision.SUMMARIZE:
                summarized.append(msg_dict)
            elif decision == MessageCompactionDecision.ARCHIVE_RAW:
                raw_archived.append(msg_dict)
            elif decision == MessageCompactionDecision.DROP_FROM_SUMMARY:
                # Drop from summary, but still archive raw so nothing is lost
                raw_archived.append(msg_dict)
            elif decision == MessageCompactionDecision.KEEP_RAW:
                # Should not happen because boundary protects KEEP_RAW,
                # but if it does, move back to remaining
                remaining_raw.insert(0, msg)

        # 5. Generate summary
        summary: str | None = None
        if summarized:
            summary = await self._summary_strategy.summarize(summarized, context, reason)

        # 6. Archive to history
        archived = False
        all_pruned: Sequence[ChatMessage | dict[str, Any]] = summarized + raw_archived
        if all_pruned and self._history_manager is not None and self._archive_strategy is not None:
            compression_result = CompressionResult(
                summary=summary or "",
                pruned_messages=all_pruned,
                remaining_messages=[_to_dict(m) for m in remaining_raw],
            )
            try:
                await self._archive_strategy.archive(
                    context,
                    all_pruned,
                    compression_result,
                    self._history_manager,
                )
                archived = True
            except Exception:
                logger.exception(
                    "Archive failed in compaction pipeline for context %s",
                    context.session_id,
                )
                # Archive failure must not abort compaction; caller
                # (e.g. AutoCompact) checks ``archived`` to decide whether
                # it is safe to overwrite short-term storage.

        return MemoryCompactionResult(
            remaining_messages=[_to_dict(m) for m in remaining_raw],
            pruned_messages=[_to_dict(m) for m in pruned_raw],
            summarized_messages=summarized,
            raw_archived_messages=raw_archived,
            dropped_messages=dropped,
            summary=summary,
            archived=archived,
        )


def _to_dict(msg: ChatMessage | dict[str, Any]) -> dict[str, Any]:
    return msg.to_dict() if isinstance(msg, ChatMessage) else dict(msg)
