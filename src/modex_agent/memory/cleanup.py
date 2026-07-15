"""Session cleanup function — prunes old messages and optionally archives them.

This is a standalone async function that handles:
1. Trigger check (token pressure: non-system session tokens exceed max_context_tokens * max_token_ratio)
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

from modex_agent.core.message import ChatMessage
from modex_agent.core.scope import MemoryContext
from modex_agent.core.types import MessageRole
from modex_agent.memory.archive_models import ArchiveGenerationResult
from modex_agent.memory.core.layers import (
    ArchiveMemoryManager,
    SessionMemoryManager,
    UserRetentionBuffer,
)
from modex_agent.memory.core.models import CompressionReason
from modex_agent.memory.pruned.manager import PrunedManager
from modex_agent.memory.sanitizer import (
    DefaultSessionToolChainSanitizer,
    ToolChainSanitizationMode,
)
from modex_agent.memory.token_estimator import (
    CharTokenEstimator,
    TokenEstimator,
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
    generation: ArchiveGenerationResult | None = None


# ── Phase helpers ──────────────────────────────────────────────────────────────


async def _prepare_cleanup_phase(
    session: SessionMemoryManager,
    context: MemoryContext,
    max_context_tokens: int | None,
    max_token_ratio: float,
    keep_ratio: float,
    max_backups: int,
    estimator: TokenEstimator,
) -> _CleanupPlan | None:
    """Phase 1: trigger check → backup → sanitize → compute keep/prune boundary.

    Returns ``None`` when cleanup is not triggered.  On success returns a
    :class:`_CleanupPlan` containing sanitized messages and the boundary split.
    """
    all_messages = await session.get_all_messages(context)
    total_count = len(all_messages)

    # Trigger on the ChatMessage objects directly (reads cached token_count +
    # role via .get). The full to_dict is deferred to after the trigger, so the
    # common under-budget path never serializes the session.
    trigger_reason = _check_trigger(all_messages, estimator, max_context_tokens, max_token_ratio)
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

    keep_target_tokens = max(1, int((max_context_tokens or 0) * keep_ratio))
    keep_messages, pruned_messages = _compute_boundary(sanitized, keep_target_tokens, estimator)

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
    """Phase 2: generate typed archive content and commit it through the manager.

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

    session_id = context.session_id
    try:
        result = await archive_agent.generate(
            pruned_messages=list(pruned_messages),
        )
        documents = result.documents
        if not any(
            content.strip()
            for content in (
                documents.context,
                documents.knowledge,
                documents.index,
            )
        ):
            logger.info(
                "Archive generation skipped: generated documents are empty. session=%s",
                session_id,
            )
            return _ArchiveOutcome(
                generated=False,
                skipped=True,
                next_archive_id=0,
            )
        if archive_storage is not None:
            bundle = await archive.append_bundle(context, result.writes)
            await archive_storage.write_archive_file(
                bundle.archive_id, "context.md", result.documents.context
            )
            await archive_storage.write_archive_file(
                bundle.archive_id, "knowledge.md", result.documents.knowledge
            )
            await archive_storage.write_archive_file(
                bundle.archive_id, "index.md", result.documents.index
            )
        else:
            bundle = await archive.append_generation(context, result)
        logger.info(
            "Archive generated: archive_id=%d session=%s",
            bundle.archive_id,
            session_id,
        )
        return _ArchiveOutcome(
            generated=True,
            skipped=False,
            next_archive_id=bundle.archive_id,
            generation=result,
        )
    except Exception:
        logger.warning(
            "Archive generation failed: session=%s",
            session_id,
            exc_info=True,
        )

    return _ArchiveOutcome(
        generated=False,
        skipped=True,
        next_archive_id=0,
    )


async def _write_pruned_phase(
    pruned_manager: PrunedManager | None,
    archive_storage: DirArchiveStorage | None,
    pruned_messages: list[dict[str, Any]],
    archive_generated: bool,
    next_archive_id: int,
    context: MemoryContext,
    generation: ArchiveGenerationResult | None = None,
) -> None:
    """Phase 3: write raw pruned content, enriching with archive topic if available."""
    if pruned_manager is None or not pruned_messages:
        return

    pruned_topic: str | None = None
    if archive_generated and generation is not None:
        pruned_topic = generation.documents.topic
    elif archive_generated and archive_storage is not None and next_archive_id > 0:
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
    new_revision = await session.retain_messages(
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
    max_context_tokens: int | None = None,
    max_token_ratio: float = 0.85,
    keep_ratio: float = 0.3,
    max_backups: int = 10,
    user_retention: UserRetentionBuffer | None = None,
    pruned_manager: PrunedManager | None = None,
    archive_agent: ArchiveGenerator | None = None,
    archive_storage: DirArchiveStorage | None = None,
    on_archive_generated: Callable[[], Awaitable[None]] | None = None,
    on_triggered: Callable[[MemoryContext, CompressionReason], Awaitable[None]] | None = None,
    token_estimator: TokenEstimator | None = None,
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
    estimator = token_estimator or CharTokenEstimator()
    plan = await _prepare_cleanup_phase(
        session,
        context,
        max_context_tokens,
        max_token_ratio,
        keep_ratio,
        max_backups,
        estimator,
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

    # Trigger confirmed and a real cleanup is about to run — notify listeners
    # BEFORE the (potentially slow) archive-generation LLM call so an observer
    # can tell the user "consolidating memory, please wait".
    if on_triggered is not None:
        try:
            await on_triggered(context, plan.trigger_reason)
        except Exception:
            logger.warning("on_triggered callback failed: session=%s", context.session_id)

    # Phase 2: archive generation
    archive_outcome = await _generate_archive_phase(
        archive_agent,
        archive_storage,
        archive,
        plan.pruned_messages,
        context,
    )

    # Phase 3: pruned content write
    await _write_pruned_phase(
        pruned_manager,
        archive_storage,
        plan.pruned_messages,
        archive_outcome.generated,
        archive_outcome.next_archive_id,
        context,
        archive_outcome.generation,
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
        archive_storage,
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


_MessageLike = dict[str, Any] | ChatMessage

_SYSTEM_ROLE = str(MessageRole.SYSTEM)


def _resolve_message_tokens(
    message: _MessageLike,
    estimator: TokenEstimator,
) -> int:
    """Return a message's token count: cached if a sane positive int, else recompute.

    Works on either a persisted dict or a ``ChatMessage`` (both expose ``.get``).
    The cache is authoritative because the SAME estimator stamps ``token_count``
    at append time. A missing, non-int, or non-positive cached value is treated
    as corrupt and recomputed transiently (not written back).
    """
    cached = message.get("token_count")
    if isinstance(cached, int) and cached > 0:
        return cached
    return estimator.estimate_message(message)


def _sum_tokens(messages: Sequence[_MessageLike], estimator: TokenEstimator) -> int:
    return sum(_resolve_message_tokens(m, estimator) for m in messages)


def _check_trigger(
    messages: Sequence[_MessageLike],
    estimator: TokenEstimator,
    max_context_tokens: int | None,
    max_token_ratio: float,
) -> CompressionReason | None:
    """Fire compression when NON-SYSTEM session tokens exceed max_context_tokens * ratio.

    System-role tokens are excluded from session pressure (per ADR-0009): the
    system prompt size is hard to predict and is regulated separately by the
    request-time TokenBudgetGovernance. Reads cached ``token_count`` + ``role``
    via ``.get``, so callers may pass ``ChatMessage`` objects without serializing
    them — the common under-budget path does zero ``to_dict`` work.
    """
    if max_context_tokens is None:
        return None
    threshold = max_context_tokens * max_token_ratio
    pressure = sum(
        _resolve_message_tokens(m, estimator)
        for m in messages
        if str(m.get("role", "")) != _SYSTEM_ROLE
    )
    return CompressionReason.TOKEN_PRESSURE if pressure > threshold else None


def _compute_boundary(
    messages: list[dict[str, Any]],
    keep_target_tokens: int,
    estimator: TokenEstimator,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split messages into (keep, pruned) by accumulating tokens from the tail.

    Walk backward summing each message's resolved token count until the next
    message would exceed ``keep_target_tokens``; keep everything from there to
    the end. ``keep_target_tokens`` is a SOFT target with a HARD floor of one
    message: the most recent message is always retained even if it alone
    exceeds the target. If the cut splits a tool chain, the chain is evicted
    forward into pruned (never kept orphaned), which only ever shrinks keep.
    """
    total = len(messages)
    if total == 0:
        return [], []

    accumulated = 0
    boundary = total - 1  # exclusive start of keep region; tail msg always kept

    for i in range(total - 1, -1, -1):
        msg_tokens = _resolve_message_tokens(messages[i], estimator)
        if i < total - 1 and accumulated + msg_tokens > keep_target_tokens:
            boundary = i + 1
            break
        accumulated += msg_tokens
        boundary = i

    boundary = _adjust_boundary_for_tool_chains(messages, boundary)

    keep = messages[boundary:]
    pruned = messages[:boundary]
    return keep, pruned


def _adjust_boundary_for_tool_chains(
    messages: list[dict[str, Any]],
    boundary: int,
) -> int:
    """If the boundary splits a tool chain, evict it FORWARD into pruned.

    The kept region (messages[boundary:]) must never contain a tool result
    whose assistant tool_call was pruned. Move the boundary forward (toward
    the tail) past any leading tool results whose calls lie before the
    boundary, so the whole chain lands in pruned and gets archived. The hard
    keep-target cap is never exceeded by this adjustment (it only shrinks
    keep).
    """
    while 0 < boundary < len(messages):
        first = messages[boundary]
        if first.get("role") != MessageRole.TOOL:
            break
        tool_call_id = first.get("tool_call_id")
        owner_pruned = any(
            messages[j].get("role") == MessageRole.ASSISTANT
            and any(tc.get("id") == tool_call_id for tc in (messages[j].get("tool_calls") or []))
            for j in range(boundary)
        )
        if owner_pruned:
            boundary += 1  # evict this orphan tool result into pruned
            continue
        break
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

        bundle = await factory(context)
        store = bundle.messages
        directory: Path | None = getattr(store, "directory", None)
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
