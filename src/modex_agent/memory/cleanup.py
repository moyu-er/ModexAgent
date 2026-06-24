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
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from modex_agent.core.types import MessageRole
from modex_agent.memory.core.layers import (
    ArchiveMemoryManager,
    SessionMemoryManager,
    UserRetentionBuffer,
)
from modex_agent.memory.core.models import CompressionReason
from modex_agent.memory.core.scope import MemoryContext
from modex_agent.memory.pruned.manager import PrunedManager
from modex_agent.memory.sanitizer import (
    DefaultSessionToolChainSanitizer,
    ToolChainSanitizationMode,
)
from modex_agent.memory.user_buffer import UserBufferEntry
from modex_agent.utils.timezone import get_user_timezone

if TYPE_CHECKING:
    from modex_agent.agents.summarizer.abc import ArchiveGenerator
    from modex_agent.memory.stores.dir_archive import DirArchiveStorage

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


async def _write_pruned_content(
    pruned_manager: PrunedManager,
    pruned_messages: list[dict[str, Any]],
    context: MemoryContext,
    *,
    topic: str | None = None,
) -> None:
    """Write raw pruned messages to the pruned store.

    Always called when messages are pruned, regardless of archive success.
    When *topic* is provided (e.g. from archive index.md), it is used as
    the index entry topic instead of the default time-range fallback.
    """
    try:
        await pruned_manager.write_pruned(
            pruned_messages,
            topic=topic,
            cleanup_time=datetime.now(get_user_timezone()),
            session_id=context.session_id or "",
        )
    except Exception:
        logger.warning("Pruned content write failed", exc_info=True)


# ── Internal result types ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class _CleanupPlan:
    """Result of the prepare phase: sanitized messages with keep/prune split."""

    trigger_reason: CompressionReason
    total_count: int
    sanitized: list[dict[str, Any]]
    keep_messages: list[dict[str, Any]]
    pruned_messages: list[dict[str, Any]]


@dataclass(frozen=True)
class _ArchiveOutcome:
    """Result of archive generation phase."""

    generated: bool
    skipped: bool
    next_archive_id: int
    resolved_storage: DirArchiveStorage | None = None


# ── Phase helpers ──────────────────────────────────────────────────────────────


async def _prepare_cleanup_phase(
    session: SessionMemoryManager,
    context: MemoryContext,
    max_messages: int | None,
    max_tokens: int | None,
    keep_ratio: float,
    max_backups: int,
) -> _CleanupPlan | None:
    """Phase 1: trigger check → backup → sanitize → compute keep/prune boundary.

    Returns ``None`` when cleanup is not triggered.  On success returns a
    :class:`_CleanupPlan` containing sanitized messages and the boundary split.
    """
    all_messages = await session.get_all_messages(context)
    total_count = len(all_messages)

    trigger_reason = _check_trigger(all_messages, total_count, max_messages, max_tokens)
    if trigger_reason is None:
        return None

    logger.info(
        "Cleanup triggered: session=%s reason=%s total=%d",
        context.session_id,
        trigger_reason.value,
        total_count,
    )

    await _backup_session(session, context, all_messages, max_backups)

    all_dicts = [m.to_dict() for m in all_messages]
    sanitizer = DefaultSessionToolChainSanitizer()
    sanitization = sanitizer.sanitize(
        all_dicts,
        mode=ToolChainSanitizationMode.PERSISTENT_SESSION,
    )

    if sanitization.removed_messages:
        logger.info(
            "Sanitizer removed invalid messages: session=%s removed=%d",
            context.session_id,
            len(sanitization.removed_messages),
        )

    sanitized = sanitization.messages
    if not sanitized:
        # All messages were invalid — caller will clear session
        return _CleanupPlan(
            trigger_reason=trigger_reason,
            total_count=total_count,
            sanitized=[],
            keep_messages=[],
            pruned_messages=all_dicts,
        )

    keep_target = max(1, int(len(sanitized) * keep_ratio))
    keep_messages, pruned_messages = _compute_boundary(sanitized, keep_target)

    if not keep_messages:
        logger.warning("No safe keep boundary found: session=%s", context.session_id)
        return _CleanupPlan(
            trigger_reason=trigger_reason,
            total_count=total_count,
            sanitized=sanitized,
            keep_messages=[],
            pruned_messages=pruned_messages,
        )

    keep_messages = _resanitize_keep(keep_messages)
    return _CleanupPlan(
        trigger_reason=trigger_reason,
        total_count=total_count,
        sanitized=sanitized,
        keep_messages=keep_messages,
        pruned_messages=pruned_messages,
    )


async def _generate_archive_phase(
    archive_agent: ArchiveGenerator | None,
    archive_storage: DirArchiveStorage | None,
    archive: ArchiveMemoryManager | None,
    pruned_messages: list[dict[str, Any]],
    context: MemoryContext,
) -> _ArchiveOutcome:
    """Phase 2: resolve storage, read state, and run archive agent.

    Returns an :class:`_ArchiveOutcome` describing what happened.
    """
    if archive is None:
        logger.debug(
            "Archive generation skipped: archive layer is disabled. session=%s",
            context.session_id,
        )
        return _ArchiveOutcome(generated=False, skipped=True, next_archive_id=0)

    if archive_agent is None:
        logger.info(
            "Archive generation skipped: archive_agent not configured. session=%s",
            context.session_id,
        )
        return _ArchiveOutcome(generated=False, skipped=True, next_archive_id=0)

    if not pruned_messages:
        logger.info(
            "Archive generation skipped: no pruned messages. session=%s",
            context.session_id,
        )
        return _ArchiveOutcome(generated=False, skipped=True, next_archive_id=0)

    # Resolve archive_storage dynamically when not injected
    resolved_storage = archive_storage
    if resolved_storage is None:
        try:
            archive_dir = await archive.get_storage_path(context)
            if archive_dir is not None:
                from modex_agent.memory.stores.dir_archive import DirArchiveStorage

                resolved_storage = DirArchiveStorage(archive_dir)
                logger.info(
                    "Archive storage resolved dynamically: session=%s path=%s",
                    context.session_id,
                    archive_dir,
                )
            else:
                logger.warning(
                    "Archive storage resolution returned None: session=%s",
                    context.session_id,
                )
        except Exception:
            logger.warning(
                "Cannot resolve archive directory dynamically: session=%s",
                context.session_id,
                exc_info=True,
            )

    if resolved_storage is None:
        logger.warning(
            "Archive generation skipped: archive_agent present but storage unresolved. session=%s",
            context.session_id,
        )
        return _ArchiveOutcome(
            generated=False, skipped=True, next_archive_id=0, resolved_storage=None
        )

    session_id = context.session_id
    try:
        # Atomically reserve the next archive_id under write lock.
        # This prevents concurrent sessions from getting the same ID.
        async with resolved_storage.get_lock().write():
            state_data = await resolved_storage.read_archive_state() or {}
            next_archive_id = state_data.get("next_archive_id", 1)
            # Reserve immediately — increment and persist while holding the lock
            await resolved_storage.write_archive_state(
                {"next_archive_id": next_archive_id + 1}
            )
    except Exception:
        logger.warning(
            "Failed to read archive state: session=%s",
            session_id,
            exc_info=True,
        )
        next_archive_id = 1

    try:
        is_complete = await resolved_storage.is_archive_complete(next_archive_id)
    except Exception:
        logger.warning(
            "Archive completeness check failed: archive_id=%d session=%s",
            next_archive_id,
            session_id,
            exc_info=True,
        )
        is_complete = False

    if is_complete:
        logger.info(
            "Archive %d already complete, skipping generation. session=%s",
            next_archive_id,
            session_id,
        )
        return _ArchiveOutcome(
            generated=True,
            skipped=False,
            next_archive_id=next_archive_id,
            resolved_storage=resolved_storage,
        )

    archive_dir = resolved_storage.base_dir / str(next_archive_id)
    logger.info(
        "Starting archive generation: archive_id=%d session=%s",
        next_archive_id,
        session_id,
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
                next_archive_id,
                session_id,
                result.files_written,
            )
            return _ArchiveOutcome(
                generated=True,
                skipped=False,
                next_archive_id=next_archive_id,
                resolved_storage=resolved_storage,
            )
        logger.warning(
            "Archive generation failed: archive_id=%d session=%s error=%s",
            next_archive_id,
            session_id,
            result.error,
        )
    except Exception:
        logger.warning(
            "Archive agent crashed: archive_id=%d session=%s",
            next_archive_id,
            session_id,
            exc_info=True,
        )

    return _ArchiveOutcome(
        generated=False,
        skipped=True,
        next_archive_id=next_archive_id,
        resolved_storage=resolved_storage,
    )


async def _write_pruned_phase(
    pruned_manager: PrunedManager | None,
    archive_storage: DirArchiveStorage | None,
    pruned_messages: list[dict[str, Any]],
    archive_generated: bool,
    next_archive_id: int,
    context: MemoryContext,
) -> None:
    """Phase 3: write raw pruned content, enriching with archive topic if available."""
    if pruned_manager is None or not pruned_messages:
        return

    pruned_topic: str | None = None
    if archive_generated and archive_storage is not None and next_archive_id > 0:
        try:
            index_md = await archive_storage.read_archive_file(
                next_archive_id,
                "index.md",
            )
            if index_md and index_md.strip():
                pruned_topic = index_md.strip().split("\n")[0].strip() or None
        except Exception:
            logger.debug(
                "Failed to read archive topic for pruned entry: session=%s",
                context.session_id,
                exc_info=True,
            )

    await _write_pruned_content(
        pruned_manager,
        pruned_messages,
        context,
        topic=pruned_topic,
    )


def _extract_retention_entries(
    sanitized: list[dict[str, Any]],
    pruned_messages: list[dict[str, Any]],
) -> list[UserBufferEntry]:
    """Phase 4: extract user retention entries from pruned messages.

    Walks all *sanitized* messages; accumulates pruned user/agent messages.
    When a plain assistant appears (no tool_calls, content present), flushes
    accumulated entries as a completed turn.
    """
    if not pruned_messages:
        return []

    pruned_now = _time.time()
    boundary_idx = len(pruned_messages)
    pruned_indices = set(range(boundary_idx))
    retention_entries: list[UserBufferEntry] = []
    pending: list[dict[str, Any]] = []

    for idx, msg in enumerate(sanitized):
        role = str(msg.get("role", ""))
        # Plain assistant (no tool_calls, has content) -> completed turn barrier
        if role == MessageRole.ASSISTANT and not msg.get("tool_calls") and msg.get("content"):
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
        if idx in pruned_indices and role in {MessageRole.USER, MessageRole.AGENT}:
            pending.append(msg)

    # Flush remaining pending entries (no plain assistant after them)
    for pending_msg in pending:
        try:
            entry = UserBufferEntry.from_message(pending_msg, pruned_at=pruned_now)
            retention_entries.append(entry)
        except (ValueError, TypeError):
            pass

    return retention_entries


async def _commit_session_phase(
    session: SessionMemoryManager,
    context: MemoryContext,
    keep_messages: list[dict[str, Any]],
    pruned_messages: list[dict[str, Any]],
    retention_entries: list[UserBufferEntry],
    user_retention: UserRetentionBuffer | None,
) -> tuple[int, int] | None:
    """Phase 5: replace session messages and persist retention entries.

    Returns ``(keep_count, prune_count)`` on success, or ``None`` when a
    revision conflict prevents the commit.
    """
    revision = await session.get_revision(context)
    new_revision = await session.replace_messages_if_revision(
        context,
        keep_messages,
        revision,
    )

    if new_revision is None:
        logger.debug(
            "Cleanup commit conflict (revision changed): session=%s",
            context.session_id,
        )
        return None

    prune_count = len(pruned_messages)
    keep_count = len(keep_messages)
    logger.info(
        "Cleanup committed: session=%s kept=%d pruned=%d",
        context.session_id,
        keep_count,
        prune_count,
    )

    # Persist user retention entries
    if user_retention is not None and retention_entries:
        try:
            for entry in retention_entries:
                await user_retention.upsert_pruned_user(context, entry)
        except Exception:
            logger.warning(
                "User retention persistence failed: session=%s",
                context.session_id,
                exc_info=True,
            )

    # A plain assistant in the kept region completes ALL unfinished entries
    if user_retention is not None and keep_messages:
        last_plain_asst: str | None = None
        for msg in reversed(keep_messages):
            role = str(msg.get("role", ""))
            if role == MessageRole.ASSISTANT and not msg.get("tool_calls") and msg.get("content"):
                last_plain_asst = str(msg.get("content", ""))
                break
        if last_plain_asst is not None:
            try:
                await user_retention.mark_all_completed(context, last_plain_asst)
            except Exception:
                logger.warning(
                    "URB mark_all_completed failed: session=%s",
                    context.session_id,
                    exc_info=True,
                )

    return keep_count, prune_count


async def _advance_archive_phase(
    archive_agent: ArchiveGenerator | None,
    archive_storage: DirArchiveStorage | None,
    archive_generated: bool,
    next_archive_id: int,
    context: MemoryContext,
    on_archive_generated: Callable[[], Awaitable[None]] | None,
) -> None:
    """Phase 6: fire post-archive trigger.

    Archive state (next_archive_id) is already advanced atomically in
    Phase 2 (_generate_archive_phase) so no state write is needed here.
    """
    if archive_agent is not None and archive_storage is not None and archive_generated:
        logger.info(
            "Archive state already advanced: next_archive_id=%d session=%s",
            next_archive_id + 1,
            context.session_id,
        )

    if archive_generated and on_archive_generated is not None:
        try:
            await on_archive_generated()
        except Exception:
            logger.debug("Post-cleanup archive trigger failed", exc_info=True)


# ── Cleanup orchestrator ───────────────────────────────────────────────────────


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
    archive_agent: ArchiveGenerator | None = None,
    archive_storage: DirArchiveStorage | None = None,
    on_archive_generated: Callable[[], Awaitable[None]] | None = None,
) -> CleanupResult:
    """Clean up a session by pruning old messages and optionally archiving them.

    Orchestrates 6 phases:
        1. Prepare (trigger, backup, sanitize, boundary)
        2. Archive generation (optional)
        3. Pruned content write
        4. Retention extraction
        5. Session commit + retention persistence
        6. Archive state advance + trigger
    """
    # Phase 1: prepare
    plan = await _prepare_cleanup_phase(
        session,
        context,
        max_messages,
        max_tokens,
        keep_ratio,
        max_backups,
    )
    if plan is None:
        return CleanupResult(triggered=False)

    # Edge case: all messages invalid -> clear session
    if not plan.sanitized:
        revision = await session.get_revision(context)
        await session.replace_messages_if_revision(context, [], revision)
        return CleanupResult(
            triggered=True,
            messages_kept=0,
            messages_pruned=plan.total_count,
            archive_skipped=True,
            reason=plan.trigger_reason,
        )

    # Edge case: no safe boundary
    if not plan.keep_messages:
        return CleanupResult(
            triggered=True,
            messages_kept=plan.total_count,
            messages_pruned=0,
            archive_skipped=True,
            reason=plan.trigger_reason,
        )

    # Phase 2: archive generation
    archive_outcome = await _generate_archive_phase(
        archive_agent,
        archive_storage,
        archive,
        plan.pruned_messages,
        context,
    )

    # Use resolved_storage from Phase 2 (may have been dynamically created)
    effective_storage = archive_outcome.resolved_storage or archive_storage

    # Phase 3: pruned content write
    await _write_pruned_phase(
        pruned_manager,
        effective_storage,
        plan.pruned_messages,
        archive_outcome.generated,
        archive_outcome.next_archive_id,
        context,
    )

    # Phase 4: retention extraction
    retention_entries = (
        _extract_retention_entries(plan.sanitized, plan.pruned_messages)
        if user_retention is not None
        else []
    )

    # Phase 5: session commit + retention persistence
    commit_result = await _commit_session_phase(
        session,
        context,
        plan.keep_messages,
        plan.pruned_messages,
        retention_entries,
        user_retention,
    )
    if commit_result is None:
        # Revision conflict
        return CleanupResult(
            triggered=True,
            messages_kept=plan.total_count,
            messages_pruned=0,
            archive_skipped=True,
            reason=plan.trigger_reason,
        )
    keep_count, prune_count = commit_result

    # Phase 6: advance archive state + trigger
    await _advance_archive_phase(
        archive_agent,
        effective_storage,
        archive_outcome.generated,
        archive_outcome.next_archive_id,
        context,
        on_archive_generated,
    )

    return CleanupResult(
        triggered=True,
        messages_kept=keep_count,
        messages_pruned=prune_count,
        archive_skipped=archive_outcome.skipped,
        reason=plan.trigger_reason,
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
        from modex_agent.memory.utils import estimate_token_count
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
    if msg_at_boundary.get("role") == MessageRole.TOOL:
        # Walk backward to find the assistant that started this tool chain
        for j in range(boundary - 1, -1, -1):
            candidate = messages[j]
            if candidate.get("role") == MessageRole.ASSISTANT and candidate.get("tool_calls"):
                call_ids = {tc.get("id") for tc in candidate.get("tool_calls") or []}
                # Check if this tool result belongs to this assistant
                if msg_at_boundary.get("tool_call_id") in call_ids:
                    # Don't split: move boundary before the assistant
                    return j
            elif candidate.get("role") == MessageRole.ASSISTANT and not candidate.get("tool_calls"):
                # Plain assistant — tool chain doesn't extend further back
                break
            elif candidate.get("role") == MessageRole.USER:
                break

    # Case 2: assistant with tool_calls just before boundary, tool results at or after boundary
    if msg_before.get("role") == MessageRole.ASSISTANT and msg_before.get("tool_calls"):
        call_ids = {tc.get("id") for tc in msg_before.get("tool_calls") or []}
        # Check if any tool result for these calls is at or after boundary
        has_tool_result_after = any(
            messages[k].get("role") == MessageRole.TOOL
            and messages[k].get("tool_call_id") in call_ids
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
            context.session_id,
            backup_path.name,
            min(len(existing), max_backups),
        )
    except Exception:
        # Backup is a safety net — failure must never block cleanup.
        logger.debug("Session backup failed (cleanup will proceed)", exc_info=True)
