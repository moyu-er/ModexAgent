"""Session cleanup function — replaces MemoryCompressionCoordinator.maybe_compress().

This is a standalone async function that handles:
1. Trigger check (message count or token pressure)
2. Cleanup (sanitize tool chains, compute keep/prune boundary, commit)
3. Optional archive (generate archive from pruned messages)

It does NOT import from framework.memory.compression or framework.memory.compaction.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
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

# Per-session in-memory archive failure counters.
#
# Keyed by context.session_id.  Each session has an independent counter so a
# failing archive in one session never affects another.  The dictionary is
# bounded in practice by the number of active sessions in the process.
_archive_fail_counters: dict[str, int] = {}

# Maximum entries before a compaction pass prunes counters for sessions that
# have been reset (value 0).  This keeps the dictionary from growing without
# bound in long-running processes with ephemeral session ids.
_MAX_COUNTER_ENTRIES = 2000


def _compact_counters() -> None:
    """Drop counter entries with value 0 to bound dictionary growth.

    Called after a counter is reset to 0, so stale entries for finished
    sessions are eventually reclaimed without affecting active counters.
    """
    if len(_archive_fail_counters) <= _MAX_COUNTER_ENTRIES:
        return
    for key in list(_archive_fail_counters):
        if _archive_fail_counters[key] == 0:
            del _archive_fail_counters[key]


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
    max_backups: int = 10,
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

    # ── Step 1.5: Backup session before any mutation ────────────────────────
    await _backup_session(session, context, all_messages, max_backups)

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
    #
    # Each session has an independent failure counter keyed by session_id so
    # one session's archive failures never affect another session.
    archive_skipped = True

    if archive is not None and archive_strategy is not None and pruned_messages:
        session_id = context.session_id
        # Guard: a valid session_id is required for per-session counting.
        # Sessions without an id use a one-shot attempt without counter tracking.
        if not session_id:
            logger.warning(
                "Archive skipped: context.session_id is empty — cannot track "
                "per-session failure counter",
            )
        else:
            fail_count = _archive_fail_counters.get(session_id, 0)

            if fail_count >= archive_fail_threshold:
                logger.info(
                    "Archive skipped due to consecutive failures: session=%s "
                    "fail_count=%d threshold=%d",
                    session_id, fail_count, archive_fail_threshold,
                )
                _archive_fail_counters[session_id] = 0
                _compact_counters()
            else:
                try:
                    archive_inputs = [
                        ArchiveInputMessage.from_dict(m) for m in pruned_messages
                    ]
                    gen_result = await archive_strategy.generate(
                        archive_inputs, context, trigger_reason,
                    )
                    if gen_result.writes:
                        await archive.append_bundle(context, gen_result.writes)
                    _archive_fail_counters[session_id] = 0
                    archive_skipped = False
                except Exception:
                    _archive_fail_counters[session_id] = fail_count + 1
                    logger.warning(
                        "Archive generation failed: session=%s fail_count=%d",
                        session_id,
                        _archive_fail_counters[session_id],
                        exc_info=True,
                    )

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
    except ImportError:
        logger.debug("estimate_token_count not available, using heuristic")
        return sum(len(str(m)) // 4 for m in messages)
    else:
        return estimate_token_count(messages)


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

    if keep_target >= total:
        # Keep everything — no pruning needed
        return list(messages), []

    # Walk backward from end, counting messages
    accumulated = 0
    boundary = total  # exclusive start of keep region

    for i in range(total - 1, -1, -1):
        accumulated += 1
        if accumulated >= keep_target:
            boundary = i
            break

    # Guard: if the loop never set boundary (should not happen, but defensive)
    if boundary >= total:
        boundary = max(0, total - 1)

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


# ── Backup ───────────────────────────────────────────────────────────────────


async def _backup_session(
    session: SessionMemoryManager,
    context: MemoryContext,
    messages: Sequence[Any],
    max_backups: int,
) -> None:
    """Deep-copy session messages to a timestamped backup before cleanup.

    Resolves the underlying storage directory through duck typing so the
    mechanism works with any file-based backend (``DefaultScopedStorage``,
    future backends with a ``.directory`` attribute).  Non-file backends
    (in-memory, remote) are silently skipped.

    Backups land in ``<storage_dir>/backups/backup_<timestamp>.jsonl``.
    When the backup count exceeds *max_backups* the oldest files are pruned.
    """
    try:
        # Resolve the storage instance via the session's internal factory.
        # Duck typing keeps this backend-agnostic — non-standard session
        # managers without _storage_factory simply skip.
        factory = getattr(session, "_storage_factory", None)
        if factory is None:
            return

        storage = await factory(context)
        directory: Path | None = getattr(storage, "directory", None)
        if directory is None:
            return  # Non-file backend — nothing to copy

        backup_dir = directory / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S_%f")
        backup_path = backup_dir / f"backup_{timestamp}.jsonl"

        with backup_path.open("w", encoding="utf-8") as handle:
            for msg in messages:
                msg_dict = msg.to_dict() if hasattr(msg, "to_dict") else dict(msg)
                handle.write(json.dumps(msg_dict, ensure_ascii=False) + "\n")

        # Prune oldest backups when count exceeds limit
        existing = sorted(backup_dir.glob("backup_*.jsonl"))
        if len(existing) > max_backups:
            for old_path in existing[: len(existing) - max_backups]:
                old_path.unlink()

        logger.info(
            "Session backup created: session=%s path=%s total_backups=%d",
            context.session_id, backup_path.name, min(len(existing), max_backups),
        )
    except Exception:
        # Backup is a safety net — failure must never block cleanup.
        logger.debug("Session backup failed (cleanup will proceed)", exc_info=True)
