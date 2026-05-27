"""Session cleanup function — replaces MemoryCompressionCoordinator.maybe_compress().

This is a standalone async function that handles:
1. Trigger check (message count or token pressure)
2. Cleanup (sanitize tool chains, compute keep/prune boundary, commit)
3. Optional archive (generate archive from pruned messages)

It does NOT import from framework.memory.compression or framework.memory.compaction.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from framework.memory.archive_generation import (
    ArchiveGenerationStrategy,
    ArchiveInputMessage,
)
from framework.memory.core.layers import ArchiveMemoryManager, SessionMemoryManager
from framework.memory.core.models import CompressionReason
from framework.memory.core.scope import MemoryContext
from framework.memory.sanitizer import (
    DefaultSessionToolChainSanitizer,
    ToolChainSanitizationMode,
)

logger = logging.getLogger(__name__)

# Per-session in-memory failure counters.
_archive_fail_counters: dict[str, int] = {}


@dataclass(frozen=True)
class CleanupResult:
    """Result of a cleanup_session() call."""

    triggered: bool
    messages_kept: int = 0
    messages_pruned: int = 0
    archive_skipped: bool = False
    reason: CompressionReason | None = None


async def cleanup_session(
    *,
    session: SessionMemoryManager,
    archive: ArchiveMemoryManager | None,
    context: MemoryContext,
    max_messages: int | None = None,
    max_tokens: int | None = None,
    keep_ratio: float = 0.5,
    archive_strategy: ArchiveGenerationStrategy | None = None,
    archive_fail_threshold: int = 3,
) -> CleanupResult:
    """Clean up a session by pruning old messages and optionally archiving them.

    This replaces ``MemoryCompressionCoordinator.maybe_compress()``. It is a
    standalone async function (not a class) called directly from
    ``ScopedMessageHistory``.

    Flow:
        1. Trigger check (message count > max_messages OR estimated tokens > max_tokens)
        2. Cleanup (sanitize, plan boundary, commit)
        3. Optional archive (generate archive from pruned messages)
    """
    # ── Step 1: Trigger check ──────────────────────────────────────────────
    all_messages = await session.get_all_messages(context)
    total_count = len(all_messages)

    trigger_reason = _check_trigger(all_messages, total_count, max_messages, max_tokens)
    if trigger_reason is None:
        return CleanupResult(triggered=False)

    logger.info(
        "Cleanup triggered: session=%s reason=%s total=%d",
        context.session_id, trigger_reason.value, total_count,
    )

    # ── Step 2: Cleanup session ────────────────────────────────────────────
    # Convert to dicts for sanitizer
    all_dicts = [m.to_dict() for m in all_messages]

    # Sanitize: remove invalid tool-chain records
    sanitizer = DefaultSessionToolChainSanitizer()
    sanitization = sanitizer.sanitize(all_dicts, mode=ToolChainSanitizationMode.PERSISTENT_SESSION)

    if sanitization.removed_messages:
        logger.info(
            "Sanitizer removed invalid messages: session=%s removed=%d",
            context.session_id,
            len(sanitization.removed_messages),
        )

    sanitized = sanitization.messages
    if not sanitized:
        # All messages were invalid — clear session
        revision = await session.get_revision(context)
        await session.replace_messages_if_revision(context, [], revision)
        return CleanupResult(
            triggered=True,
            messages_kept=0,
            messages_pruned=total_count,
            archive_skipped=True,
            reason=trigger_reason,
        )

    # Plan: compute keep/prune boundary
    keep_target = max(1, int(len(sanitized) * keep_ratio))
    keep_messages, pruned_messages = _compute_boundary(sanitized, keep_target)

    if not keep_messages:
        # Could not compute a safe boundary — skip
        logger.warning("No safe keep boundary found: session=%s", context.session_id)
        return CleanupResult(
            triggered=True,
            messages_kept=total_count,
            messages_pruned=0,
            archive_skipped=True,
            reason=trigger_reason,
        )

    # Commit: replace session messages
    revision = await session.get_revision(context)
    new_revision = await session.replace_messages_if_revision(
        context, keep_messages, revision,
    )

    if new_revision is None:
        # Revision conflict — concurrent modification, skip
        logger.debug(
            "Cleanup commit conflict (revision changed): session=%s",
            context.session_id,
        )
        return CleanupResult(
            triggered=True,
            messages_kept=total_count,
            messages_pruned=0,
            archive_skipped=True,
            reason=trigger_reason,
        )

    prune_count = len(pruned_messages)
    keep_count = len(keep_messages)
    logger.info(
        "Cleanup committed: session=%s kept=%d pruned=%d",
        context.session_id, keep_count, prune_count,
    )

    # ── Step 3: Archive (optional) ─────────────────────────────────────────
    archive_skipped = True

    if archive is not None and archive_strategy is not None and pruned_messages:
        session_key = context.session_id or "default"
        fail_count = _archive_fail_counters.get(session_key, 0)

        # Check failure counter
        if fail_count >= archive_fail_threshold:
            logger.info(
                "Archive skipped due to consecutive failures: session=%s fail_count=%d threshold=%d",
                session_key, fail_count, archive_fail_threshold,
            )
            _archive_fail_counters[session_key] = 0
            archive_skipped = True
        else:
            # Try to generate and write archive
            try:
                archive_inputs = [
                    ArchiveInputMessage.from_dict(m) for m in pruned_messages
                ]
                gen_result = await archive_strategy.generate(
                    archive_inputs, context, trigger_reason,
                )
                if gen_result.writes:
                    await archive.append_bundle(context, gen_result.writes)
                _archive_fail_counters[session_key] = 0
                archive_skipped = False
            except Exception:
                _archive_fail_counters[session_key] = fail_count + 1
                logger.warning(
                    "Archive generation failed: session=%s fail_count=%d",
                    session_key,
                    _archive_fail_counters[session_key],
                    exc_info=True,
                )
    elif archive is None or archive_strategy is None:
        archive_skipped = True
    elif not pruned_messages:
        archive_skipped = True

    return CleanupResult(
        triggered=True,
        messages_kept=keep_count,
        messages_pruned=prune_count,
        archive_skipped=archive_skipped,
        reason=trigger_reason,
    )


def _check_trigger(
    messages: Sequence[Any],
    total_count: int,
    max_messages: int | None,
    max_tokens: int | None,
) -> CompressionReason | None:
    """Check whether cleanup should be triggered."""
    if max_messages is not None and total_count > max_messages:
        return CompressionReason.MESSAGE_COUNT

    if max_tokens is not None:
        estimated = _estimate_tokens(messages)
        if estimated > max_tokens:
            return CompressionReason.TOKEN_PRESSURE

    return None


def _estimate_tokens(messages: Sequence[Any]) -> int:
    """Estimate token count for a list of messages."""
    try:
        from framework.memory.utils import estimate_token_count
        return estimate_token_count(messages)
    except Exception:
        return sum(len(str(m)) // 4 for m in messages)


def _compute_boundary(
    messages: list[dict[str, Any]],
    keep_target: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Compute keep/prune boundary by walking backward from the end.

    Rules:
    - Walk backward counting messages until accumulated >= keep_target
    - Never split a tool chain: if boundary splits a tool result from its
      assistant, move boundary before the assistant
    - Always keep the most recent user message (anchor)

    Returns (keep_messages, pruned_messages).
    """
    total = len(messages)
    if total == 0:
        return [], []

    # Walk backward from end, counting messages
    accumulated = 0
    boundary = total  # exclusive start of keep region

    for i in range(total - 1, -1, -1):
        accumulated += 1
        if accumulated >= keep_target:
            boundary = i
            break

    # Ensure we don't split a tool chain
    boundary = _adjust_boundary_for_tool_chains(messages, boundary)

    # Ensure the most recent user message is kept
    boundary = _adjust_boundary_for_last_user(messages, boundary)

    keep = messages[boundary:]
    pruned = messages[:boundary]
    return keep, pruned


def _adjust_boundary_for_tool_chains(
    messages: list[dict[str, Any]],
    boundary: int,
) -> int:
    """Move boundary backward if it splits an assistant tool_call from its tool result."""
    if boundary <= 0 or boundary >= len(messages):
        return boundary

    # Check if boundary falls inside a tool chain:
    # - message at boundary is a tool result → check if preceding assistant has tool_calls
    # - message at boundary-1 is an assistant with tool_calls → ensure results are included
    msg_at_boundary = messages[boundary]
    msg_before = messages[boundary - 1]

    # Case 1: tool result at boundary, assistant with tool_calls before it
    if msg_at_boundary.get("role") == "tool":
        # Walk backward to find the assistant that started this tool chain
        for j in range(boundary - 1, -1, -1):
            candidate = messages[j]
            if candidate.get("role") == "assistant" and candidate.get("tool_calls"):
                call_ids = {tc.get("id") for tc in candidate.get("tool_calls") or []}
                # Check if this tool result belongs to this assistant
                if msg_at_boundary.get("tool_call_id") in call_ids:
                    # Don't split: move boundary before the assistant
                    return j
            elif candidate.get("role") == "assistant" and not candidate.get("tool_calls"):
                # Plain assistant — tool chain doesn't extend further back
                break
            elif candidate.get("role") == "user":
                break

    # Case 2: assistant with tool_calls just before boundary, tool results at or after boundary
    if msg_before.get("role") == "assistant" and msg_before.get("tool_calls"):
        call_ids = {tc.get("id") for tc in msg_before.get("tool_calls") or []}
        # Check if any tool result for these calls is at or after boundary
        has_tool_result_after = any(
            messages[k].get("role") == "tool" and messages[k].get("tool_call_id") in call_ids
            for k in range(boundary, min(boundary + 5, len(messages)))
        )
        if has_tool_result_after:
            # The tool results are in keep region but assistant would be pruned —
            # move boundary to include the assistant
            return boundary - 1

    return boundary


def _adjust_boundary_for_last_user(
    messages: list[dict[str, Any]],
    boundary: int,
) -> int:
    """Ensure the most recent user message is in the keep region."""
    if boundary <= 0:
        return boundary

    # Find the last user message
    last_user_idx: int | None = None
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "user":
            last_user_idx = i
            break

    if last_user_idx is None:
        return boundary

    # If the last user message would be pruned, move boundary to include it
    if last_user_idx < boundary:
        boundary = last_user_idx

    return boundary
