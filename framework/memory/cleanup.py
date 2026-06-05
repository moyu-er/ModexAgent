"""Session cleanup function — prunes old messages and optionally archives them.

This is a standalone async function that handles:
1. Trigger check (message count or token pressure)
2. Cleanup (sanitize tool chains, compute keep/prune boundary)
3. Archive agent generation (context.md, knowledge.md, index.md)
4. Pruned index refresh from archive index.md files
5. Session commit (replace messages + backup)
6. Archive_id increment
"""

from __future__ import annotations

import json
import logging
import time as _time
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime

from pathlib import Path
from typing import Any

from framework.utils.timezone import get_user_timezone

from framework.memory.core.layers import (
    ArchiveMemoryManager,
    SessionMemoryManager,
    UserRetentionBuffer,
)
from framework.memory.core.models import CompressionReason
from framework.memory.core.scope import MemoryContext
from framework.memory.user_buffer import UserBufferEntry
from framework.memory.pruned.manager import PrunedManager
from framework.memory.sanitizer import (
    DefaultSessionToolChainSanitizer,
    ToolChainSanitizationMode,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CleanupResult:
    """Result of a cleanup_session() call."""

    triggered: bool
    messages_kept: int = 0
    messages_pruned: int = 0
    archive_skipped: bool = False
    reason: CompressionReason | None = None
    user_retention_extracted: int = 0


async def _write_pruned_fallback(
    pruned_manager: Any,
    pruned_messages: list[dict[str, Any]],
    context: Any,
) -> None:
    """Fallback: write pruned index from messages directly.

    Used when archive agent generation fails — the pruned index is still
    populated from the raw messages so injection can work.
    """
    try:
        await pruned_manager.write_pruned(
            pruned_messages,
            topic=None,
            cleanup_time=datetime.now(get_user_timezone()),
            session_id=context.session_id or "",
        )
    except Exception:
        logger.warning("Pruned fallback failed", exc_info=True)


async def cleanup_session(
    *,
    session: SessionMemoryManager,
    archive: ArchiveMemoryManager | None,
    context: MemoryContext,
    max_messages: int | None = None,
    max_tokens: int | None = None,
    keep_ratio: float = 0.5,
    max_backups: int = 10,
    user_retention: UserRetentionBuffer | None = None,
    pruned_manager: PrunedManager | None = None,
    archive_agent: Any | None = None,
    archive_storage: Any | None = None,
) -> CleanupResult:
    """Clean up a session by pruning old messages and optionally archiving them.

    Flow:
        1. Trigger check + sanitize + boundary computation
        2. Archive agent generation (context.md, knowledge.md, index.md)
        3. Pruned index refresh from archive index.md
        4. Session commit (replace messages + backup)
        5. Archive_id increment
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

    # Re-sanitize keep region to guarantee tool chain integrity
    keep_messages = _resanitize_keep(keep_messages)

    # ── Step 1 (NEW): Archive agent generation ──────────────────────────
    archive_skipped = True
    archive_generated = False
    next_archive_id = 0  # stored for reuse in Step 4 (save extra I/O)

    # Trace: explain why archive step may be skipped
    if archive is None:
        logger.debug(
            "Archive generation skipped: archive layer is disabled. session=%s",
            context.session_id,
        )
    elif archive_agent is None:
        logger.info(
            "Archive generation skipped: archive_agent not configured. session=%s",
            context.session_id,
        )
    elif not pruned_messages:
        logger.info(
            "Archive generation skipped: no pruned messages. session=%s",
            context.session_id,
        )

    # Resolve archive_storage dynamically from the archive layer if not provided
    if archive_storage is None and archive is not None and archive_agent is not None:
        try:
            archive_dir = await archive.get_storage_path(context)
            if archive_dir is not None:
                from framework.memory.stores.dir_archive import DirArchiveStorage
                archive_storage = DirArchiveStorage(archive_dir)
                logger.info(
                    "Archive storage resolved dynamically: session=%s path=%s",
                    context.session_id, archive_dir,
                )
            else:
                logger.warning(
                    "Archive storage resolution returned None: session=%s",
                    context.session_id,
                )
        except Exception:
            logger.warning(
                "Cannot resolve archive directory dynamically: session=%s",
                context.session_id, exc_info=True,
            )

    if archive_agent is not None and archive_storage is not None and pruned_messages:
        session_id = context.session_id

        # Read current archive state (value reused in Step 4)
        try:
            state_data = await archive_storage.read_archive_state() or {}
            next_archive_id = state_data.get("next_archive_id", 1)
        except Exception:
            logger.warning(
                "Failed to read archive state: session=%s",
                session_id, exc_info=True,
            )
            state_data = {}
            next_archive_id = 1

        # Archive skip guarantee
        try:
            is_complete = await archive_storage.is_archive_complete(next_archive_id)
        except Exception:
            logger.warning(
                "Archive completeness check failed: archive_id=%d session=%s",
                next_archive_id, session_id, exc_info=True,
            )
            is_complete = False

        if is_complete:
            logger.info(
                "Archive %d already complete, skipping generation. session=%s",
                next_archive_id, session_id,
            )
            archive_generated = True
            archive_skipped = False
        else:
            # Run agent (retry is internal to ArchiveSummarizer)
            archive_dir = archive_storage.base_dir / str(next_archive_id)
            logger.info(
                "Starting archive generation: archive_id=%d session=%s",
                next_archive_id, session_id,
            )

            try:
                result = await archive_agent.generate(
                    pruned_messages=list(pruned_messages),
                    archive_dir=archive_dir,
                    archive_id=next_archive_id,
                )
                if result.success:
                    logger.info(
                        "Archive generated: archive_id=%d session=%s files=%s",
                        next_archive_id, session_id, result.files_written,
                    )
                    archive_generated = True
                    archive_skipped = False
                else:
                    logger.warning(
                        "Archive generation failed: archive_id=%d session=%s error=%s",
                        next_archive_id, session_id, result.error,
                    )
            except Exception:
                logger.warning(
                    "Archive agent crashed: archive_id=%d session=%s",
                    next_archive_id, session_id, exc_info=True,
                )
    elif archive_agent is not None and archive_storage is None:
        logger.warning(
            "Archive generation skipped: archive_agent present but storage unresolved. session=%s",
            context.session_id,
        )

    # ── Step 2 (NEW): Pruned index refresh ─────────────────────────────
    if pruned_manager is not None and archive_storage is not None and pruned_messages:
        if archive_generated:
            # Full refresh from archive index.md files
            logger.info(
                "Refreshing pruned index from archives: session=%s",
                context.session_id,
            )
            try:
                count = await pruned_manager.refresh_from_archives(
                    archive_storage, session_id=context.session_id or "",
                )
                logger.info(
                    "Pruned index refreshed: session=%s entries=%d",
                    context.session_id, count,
                )
            except Exception:
                logger.warning(
                    "Pruned index refresh failed: session=%s",
                    context.session_id, exc_info=True,
                )
                # Fallback to raw-message path
                await _write_pruned_fallback(pruned_manager, pruned_messages, context)
        else:
            # Archive failed — fallback
            logger.info(
                "Pruned index using fallback (archive not generated): session=%s",
                context.session_id,
            )
            await _write_pruned_fallback(pruned_manager, pruned_messages, context)
    elif pruned_manager is not None and pruned_messages:
        logger.info(
            "Pruned index using fallback (archive storage unavailable): session=%s",
            context.session_id,
        )
        await _write_pruned_fallback(pruned_manager, pruned_messages, context)

    # Extract user retention entries from pruned messages
    # Walk all sanitized messages; accumulate pruned user/agent messages.
    # When a plain assistant appears (no tool_calls, content present),
    # flush accumulated entries as a completed turn.
    pruned_now = _time.time()
    boundary_idx = len(pruned_messages)
    pruned_indices = set(range(boundary_idx))
    retention_entries: list[UserBufferEntry] = []
    pending: list[dict[str, Any]] = []

    if user_retention is not None and pruned_messages:
        for idx, msg in enumerate(sanitized):
            role = str(msg.get("role", ""))
            # Plain assistant (no tool_calls, has content) -> completed turn barrier
            if role == "assistant" and not msg.get("tool_calls") and msg.get("content"):
                if pending:
                    asst_content = str(msg.get("content", ""))
                    for pending_msg in pending:
                        try:
                            entry = UserBufferEntry.from_message(pending_msg, pruned_at=pruned_now)
                            entry = replace(entry, completing_assistant_content=asst_content)
                            retention_entries.append(entry)
                        except (ValueError, TypeError):
                            pass
                    pending.clear()
                continue
            # Accumulate pruned user/agent messages
            if idx in pruned_indices and role in {"user", "agent"}:
                pending.append(msg)

        # Flush remaining pending entries (no plain assistant after them)
        for pending_msg in pending:
            try:
                entry = UserBufferEntry.from_message(pending_msg, pruned_at=pruned_now)
                retention_entries.append(entry)
            except (ValueError, TypeError):
                pass

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

    # Persist user retention entries
    if user_retention is not None and retention_entries:
        try:
            for entry in retention_entries:
                await user_retention.upsert_pruned_user(context, entry)
        except Exception:
            logger.warning(
                "User retention persistence failed: session=%s",
                context.session_id, exc_info=True,
            )

    # A plain assistant in the kept region completes ALL unfinished entries
    # (both newly created and leftover from previous cleanups).
    if user_retention is not None and keep_messages:
        last_plain_asst: str | None = None
        for msg in reversed(keep_messages):
            role = str(msg.get("role", ""))
            if (role == "assistant"
                    and not msg.get("tool_calls")
                    and msg.get("content")):
                last_plain_asst = str(msg.get("content", ""))
                break
        if last_plain_asst is not None:
            try:
                await user_retention.mark_all_completed(context, last_plain_asst)
            except Exception:
                logger.warning(
                    "URB mark_all_completed failed: session=%s",
                    context.session_id, exc_info=True,
                )

    # ── Step 5: Archive_id increment ──────────────────────────────────────
    if archive_agent is not None and archive_storage is not None and archive_generated:
        try:
            await archive_storage.write_archive_state(
                {"next_archive_id": next_archive_id + 1}
            )
            logger.info(
                "Archive state advanced: next_archive_id=%d session=%s",
                next_archive_id + 1, context.session_id,
            )
        except Exception:
            logger.warning(
                "Archive state increment failed: session=%s",
                context.session_id, exc_info=True,
            )

    return CleanupResult(
        triggered=True,
        messages_kept=keep_count,
        messages_pruned=prune_count,
        archive_skipped=archive_skipped,
        reason=trigger_reason,
        user_retention_extracted=len(retention_entries),
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

    # NOTE: we do NOT force the keep region to start with a user message.
    # The sanitizer (_resanitize_keep) removes incomplete tool chains,
    # and governance handles final API-legality (e.g. TokenBudgetGovernance
    # ensures the visible context starts with a user if the provider requires it).

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


def _resanitize_keep(keep_messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Re-sanitize keep region to guarantee tool chain integrity."""
    if not keep_messages:
        return keep_messages
    sanitizer = DefaultSessionToolChainSanitizer()
    result = sanitizer.sanitize(keep_messages, mode=ToolChainSanitizationMode.PERSISTENT_SESSION)
    return result.messages


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

        timestamp = datetime.now(get_user_timezone()).strftime("%Y%m%d_%H%M%S_%f")
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
