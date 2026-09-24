"""Session cleanup function — prunes old messages, generates compact summary,
and optionally archives them.

This is a standalone async function that handles 5 phases:
1. Prepare (trigger check on non-system session token pressure; backup;
   sanitize tool chains; compute keep/prune boundary)
2. Compact generation (LLM single-call summary of pruned messages)
3. Session commit (replace messages with [compact_summary] + [tail])
4. Pruned catalog write (raw transcripts + topic from compact summary)
5. Archive generation (optional, default off — context.md + knowledge.md;
   archive state advanced atomically inside this phase; DreamEngine polling
   is the only archive-consolidation trigger)
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from modex_agent.core.message import (
    ChatMessage,
    ImageUrlPart,
    MessageRole,
    TextPart,
    render_content_part_ref,
)
from modex_agent.memory.archive_models import ArchiveGenerationResult
from modex_agent.memory.budget import ContextBudget
from modex_agent.memory.core.layers import (
    ArchiveMemoryManager,
    SessionMemoryManager,
)
from modex_agent.memory.core.models import CompressionReason
from modex_agent.memory.hooks import (
    LlmUsage,
    MemoryHookContext,
    MemoryHookPoint,
    MemoryHookRunner,
)
from modex_agent.memory.pruned.manager import PrunedManager
from modex_agent.memory.sanitizer import (
    DefaultSessionToolChainSanitizer,
    ToolChainSanitizationMode,
)
from modex_agent.memory.scope import MemoryContext
from modex_agent.memory.token_estimator import (
    CharTokenEstimator,
    TokenEstimator,
)
from modex_agent.utils.timezone import get_user_timezone

if TYPE_CHECKING:
    from modex_agent.agents.summarizer.abc import ArchiveGenerator
    from modex_agent.agents.summarizer.session_compactor import SessionCompactorAgent
    from modex_agent.memory.stores.dir_archive import DirArchiveStorage

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CleanupResult:
    """Result of a cleanup_session() call."""

    triggered: bool
    messages_kept: int = 0
    messages_pruned: int = 0
    tokens_before: int = 0
    tokens_after: int = 0
    archive_skipped: bool = False
    compact_generated: bool = False
    reason: CompressionReason | None = None
    usage: LlmUsage | None = None
    duration_ms: float = 0.0
    source: CompactionSource | None = None


class CompactionSource(StrEnum):
    """Origin of a ``compact_session`` invocation (which trigger face fired).

    Defined here, next to the cleanup engine and its ``CleanupResult.source``
    field, rather than in ``core/system.py``: the engine owns the result type
    the source is written into, and both the ABC (``core/system.py``) and the
    append caller (``history.py``) import it from here without cycles.
    """

    PRE_LLM = "pre_llm"
    POST_APPEND = "post_append"


# ── Tail keep budget (PRD §4.4.7: absolute token budget, not a config knob) ──

#: Fraction of the effective context window reserved for the kept tail.
_TAIL_KEEP_RATIO = 0.25

#: Absolute clamp bounds for the tail keep budget (tokens), opencode-style:
#: small windows still keep a usable tail; huge windows never keep more than
#: a bounded tail so compaction actually reclaims room.
_TAIL_KEEP_MIN_TOKENS = 2_000
_TAIL_KEEP_MAX_TOKENS = 15_000


def _tail_keep_budget(budget: ContextBudget) -> int:
    """Tail keep budget: ``clamp(usable × 0.25, 2000, 15000)`` tokens.

    ``usable`` is the effective context window (limit minus the model's
    output reservation). The tail is an absolute token budget regardless of
    window size (PRD §4.4.7), not a ratio of it.
    """
    usable = max(1, (budget.max_context_tokens or 0) - budget.max_output_tokens)
    return min(_TAIL_KEEP_MAX_TOKENS, max(_TAIL_KEEP_MIN_TOKENS, int(usable * _TAIL_KEEP_RATIO)))


# ── Internal result types ──────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _CleanupPlan:
    """Result of the prepare phase: sanitized messages with keep/prune split."""

    trigger_reason: CompressionReason
    total_count: int
    sanitized: list[dict[str, Any]]
    keep_messages: list[dict[str, Any]]
    pruned_messages: list[dict[str, Any]]


@dataclass(frozen=True, slots=True)
class _ArchiveOutcome:
    """Result of archive generation phase."""

    generated: bool
    skipped: bool
    next_archive_id: int
    generation: ArchiveGenerationResult | None = None


@dataclass(frozen=True, slots=True)
class _CompactOutcome:
    """Result of compact generation phase."""

    generated: bool
    summary: str = ""
    topic: str | None = None
    usage: LlmUsage | None = None


# ── Phase helpers ──────────────────────────────────────────────────────────────


async def _prepare_cleanup_phase(
    session: SessionMemoryManager,
    context: MemoryContext,
    budget: ContextBudget,
    max_token_ratio: float,
    max_backups: int,
    estimator: TokenEstimator,
) -> tuple[_CleanupPlan | None, int]:
    """Phase 1: trigger check → backup → sanitize → compute keep/prune boundary.

    The tail keep target is the absolute token budget
    ``clamp(usable × 0.25, 2000, 15000)`` (PRD §4.4.7), not a ratio of the
    window.

    Returns the optional cleanup plan and the estimated content-token count
    before pruning.
    """
    all_messages = await session.get_all_messages(context)
    total_count = len(all_messages)
    tokens_before = _estimate_content_tokens(all_messages, estimator)

    trigger_reason = check_cleanup_trigger(
        all_messages,
        estimator,
        budget.max_context_tokens,
        max_token_ratio,
        budget.max_output_tokens,
    )
    if trigger_reason is None:
        return None, tokens_before

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
        return (
            _CleanupPlan(
                trigger_reason=trigger_reason,
                total_count=total_count,
                sanitized=[],
                keep_messages=[],
                pruned_messages=all_dicts,
            ),
            tokens_before,
        )

    keep_target_tokens = _tail_keep_budget(budget)
    keep_messages, pruned_messages = _compute_boundary(sanitized, keep_target_tokens, estimator)

    if not keep_messages:
        logger.warning("No safe keep boundary found: session=%s", context.session_id)
        return (
            _CleanupPlan(
                trigger_reason=trigger_reason,
                total_count=total_count,
                sanitized=sanitized,
                keep_messages=[],
                pruned_messages=pruned_messages,
            ),
            tokens_before,
        )

    keep_messages = _resanitize_keep(keep_messages)
    return (
        _CleanupPlan(
            trigger_reason=trigger_reason,
            total_count=total_count,
            sanitized=sanitized,
            keep_messages=keep_messages,
            pruned_messages=pruned_messages,
        ),
        tokens_before,
    )


async def _compact_generation_phase(
    compactor: SessionCompactorAgent | None,
    pruned_messages: list[dict[str, Any]],
    context: MemoryContext,
    budget: ContextBudget,
) -> _CompactOutcome:
    """Phase 2: generate compact summary from pruned messages.

    Extracts previous compact summary from pruned messages (COMPACT role),
    removes it from the message list, serializes remaining messages to plain
    text, and calls the SessionCompactorAgent to generate a structured summary.
    The turn's ``budget`` is handed to the compactor so it can size its
    summarization calls (single-pass vs segmented map+reduce) against the
    CURRENT model's window.

    Returns ``_CompactOutcome(generated=False)`` when compactor is None or
    when generation fails — the caller proceeds without a compact summary
    (degraded mode: only tail is kept, no summary).
    """
    if compactor is None:
        return _CompactOutcome(generated=False)

    if not pruned_messages:
        return _CompactOutcome(generated=False)

    # Extract previous compact summary (COMPACT role messages).
    previous_summary: str | None = None
    messages_for_compact: list[dict[str, Any]] = []
    for msg in pruned_messages:
        if str(msg.get("role", "")) == str(MessageRole.COMPACT):
            content = msg.get("content", "")
            if isinstance(content, str) and content.strip():
                previous_summary = content
        else:
            messages_for_compact.append(msg)

    if not messages_for_compact and not previous_summary:
        return _CompactOutcome(generated=False)

    try:
        compaction = await compactor.compact(
            messages=messages_for_compact,
            previous_summary=previous_summary,
            session_id=context.session_id or "session-compactor",
            budget=budget,
        )
    except Exception:
        logger.warning(
            "Compact generation failed: session=%s",
            context.session_id,
            exc_info=True,
        )
        return _CompactOutcome(generated=False)

    summary = compaction.summary
    if not summary or not summary.strip():
        logger.warning("Compact generation returned empty summary: session=%s", context.session_id)
        return _CompactOutcome(generated=False, usage=compaction.usage)

    topic = compactor.extract_topic(summary)
    logger.info(
        "Compact generated: session=%s topic=%s summary_len=%d",
        context.session_id,
        topic or "(none)",
        len(summary),
    )
    return _CompactOutcome(
        generated=True,
        summary=summary,
        topic=topic,
        usage=compaction.usage,
    )


async def _commit_session_phase(
    session: SessionMemoryManager,
    context: MemoryContext,
    keep_messages: list[dict[str, Any]],
    pruned_messages: list[dict[str, Any]],
    compact_outcome: _CompactOutcome,
    estimator: TokenEstimator,
) -> tuple[int, int, int] | None:
    """Phase 3: replace session messages with [compact_summary] + [tail].

    Returns keep count, prune count, and estimated remaining content tokens on
    success, or ``None`` when a revision conflict prevents the commit.
    """
    # Build the final keep list: compact summary (if generated) + tail.
    final_keep = list(keep_messages)
    if compact_outcome.generated and compact_outcome.summary:
        compact_msg: dict[str, Any] = {
            "role": str(MessageRole.COMPACT),
            "content": compact_outcome.summary,
            # Stamp token_count so the next boundary computation is accurate
            # instead of falling back to a transient re-estimate.
            "token_count": estimator.estimate_text(compact_outcome.summary)
            + TokenEstimator.MESSAGE_OVERHEAD,
        }
        final_keep = [compact_msg] + final_keep

    revision = await session.get_revision(context)
    new_revision = await session.retain_messages(
        context,
        final_keep,
        revision,
    )

    if new_revision is None:
        logger.debug(
            "Cleanup commit conflict (revision changed): session=%s",
            context.session_id,
        )
        return None

    prune_count = len(pruned_messages)
    keep_count = len(final_keep)
    logger.info(
        "Cleanup committed: session=%s kept=%d pruned=%d compact=%s",
        context.session_id,
        keep_count,
        prune_count,
        compact_outcome.generated,
    )
    return keep_count, prune_count, _estimate_content_tokens(final_keep, estimator)


async def _write_pruned_phase(
    pruned_manager: PrunedManager | None,
    pruned_messages: list[dict[str, Any]],
    context: MemoryContext,
    compact_topic: str | None = None,
) -> None:
    """Phase 4: write raw pruned content to the pruned catalog.

    Topic is sourced from the compact summary's ``## Objective`` section.
    Falls back to ``None`` (time-range fallback in PrunedManager) when
    compact topic is unavailable.
    """
    if pruned_manager is None or not pruned_messages:
        return

    try:
        await pruned_manager.write_pruned(
            pruned_messages,
            topic=compact_topic,
            cleanup_time=datetime.now(get_user_timezone()),
            session_id=context.session_id or "",
        )
    except Exception:
        logger.warning("Pruned content write failed", exc_info=True)


async def _generate_archive_phase(
    archive_agent: ArchiveGenerator | None,
    archive_storage: DirArchiveStorage | None,
    archive: ArchiveMemoryManager | None,
    pruned_messages: list[dict[str, Any]],
    context: MemoryContext,
) -> _ArchiveOutcome:
    """Phase 5: generate typed archive content (optional, default off).

    Generates ``context.md`` + ``knowledge.md`` from pruned messages.

    Archive state (``next_archive_id``) is advanced atomically inside this
    phase via ``archive.append_bundle`` / ``archive.append_generation``;
    there is no separate trigger phase. DreamEngine consolidation runs on
    its own polling loop (``background.py``) and is the only archive-
    consolidation trigger.
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
                documents.core,
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
                bundle.archive_id, "knowledge.md", result.documents.core
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


# ── Cleanup orchestrator ───────────────────────────────────────────────────────


async def _dispatch_cleanup_finished(
    hook_runner: MemoryHookRunner | None,
    *,
    session: SessionMemoryManager,
    archive: ArchiveMemoryManager | None,
    context: MemoryContext,
    pruned_manager: PrunedManager | None,
    result: CleanupResult,
) -> None:
    """Dispatch CLEANUP_FINISHED to the hook runner before a triggered return.

    Called before every ``triggered=True`` return in :func:`cleanup_session`.
    No-op when ``hook_runner`` is ``None``. Note that ``triggered=True`` does
    NOT guarantee pruning — the all-invalid, no-safe-boundary, and
    revision-conflict paths all return ``triggered=True`` with
    ``messages_pruned=0``.
    """
    if hook_runner is None:
        return
    finished_ctx = MemoryHookContext(
        session_manager=session,
        memory_context=context,
        cleanup_result=result,
        compression_reason=result.reason,
        archive_manager=archive,
        pruned_manager=pruned_manager,
    )
    await hook_runner.dispatch(MemoryHookPoint.CLEANUP_FINISHED, finished_ctx)


async def cleanup_session(
    *,
    session: SessionMemoryManager,
    archive: ArchiveMemoryManager | None,
    context: MemoryContext,
    budget: ContextBudget,
    source: CompactionSource | None = None,
    compactor: SessionCompactorAgent | None = None,
    max_token_ratio: float = 0.85,
    max_backups: int = 10,
    pruned_manager: PrunedManager | None = None,
    archive_agent: ArchiveGenerator | None = None,
    archive_storage: DirArchiveStorage | None = None,
    hook_runner: MemoryHookRunner | None = None,
    token_estimator: TokenEstimator | None = None,
) -> CleanupResult:
    """Clean up a session by pruning old messages and generating a compact summary.

    ``budget`` is the turn's effective budget (limit + output reservation in
    one typed value; a ``None`` limit means no trigger ever fires). It drives
    the trigger check, the tail keep budget, and the compactor's
    summarization call sizing.

    Orchestrates 5 phases:
        1. Prepare (trigger, backup, sanitize, boundary)
        2. Compact generation (LLM summary of pruned messages)
        3. Session commit ([compact_summary] + [tail])
        4. Pruned catalog write (topic from compact summary)
        5. Archive generation (optional, default off — context.md + knowledge.md;
           archive state advanced atomically inside this phase; DreamEngine
           polling is the only archive-consolidation trigger)

    Lifecycle hook dispatch (when ``hook_runner`` is provided):
        - ``CLEANUP_TRIGGERED`` fires once after trigger is confirmed and a
          real cleanup is about to run — AFTER the three early returns
          (under-threshold, all-invalid, no-safe-boundary) and BEFORE phase 2
          (compact generation). Only the revision-conflict and normal paths
          reach this dispatch point.
        - ``CLEANUP_FINISHED`` fires before every ``triggered=True`` return
          (4 return points: all-invalid, no-safe-boundary, revision-conflict,
          normal). Note that ``triggered=True`` does NOT guarantee pruning.

    An unhandled cleanup exception does NOT synthesize a finished event —
    only the four explicit ``triggered=True`` returns dispatch FINISHED.

    ``source`` labels the trigger face (write-side append vs read-side
    pre-LLM) and is carried on every returned ``CleanupResult``; ``None``
    means the caller did not label the invocation.
    """
    started = time.monotonic()
    # Phase 1: prepare
    estimator = token_estimator or CharTokenEstimator()
    plan, tokens_before = await _prepare_cleanup_phase(
        session,
        context,
        budget,
        max_token_ratio,
        max_backups,
        estimator,
    )
    if plan is None:
        return CleanupResult(
            triggered=False,
            tokens_before=tokens_before,
            tokens_after=tokens_before,
            source=source,
        )

    # Edge case: all messages invalid -> clear session.
    # triggered=True but pruned=total; FINISHED dispatches, TRIGGERED does not.
    if not plan.sanitized:
        revision = await session.get_revision(context)
        await session.replace_messages_if_revision(context, [], revision)
        result = CleanupResult(
            triggered=True,
            messages_kept=0,
            messages_pruned=plan.total_count,
            tokens_before=tokens_before,
            tokens_after=0,
            archive_skipped=True,
            reason=plan.trigger_reason,
            duration_ms=(time.monotonic() - started) * 1000,
            source=source,
        )
        await _dispatch_cleanup_finished(
            hook_runner,
            session=session,
            archive=archive,
            context=context,
            pruned_manager=pruned_manager,
            result=result,
        )
        return result

    # Edge case: no safe boundary.
    # triggered=True but pruned=0; FINISHED dispatches, TRIGGERED does not.
    if not plan.keep_messages:
        result = CleanupResult(
            triggered=True,
            messages_kept=plan.total_count,
            messages_pruned=0,
            tokens_before=tokens_before,
            tokens_after=tokens_before,
            archive_skipped=True,
            reason=plan.trigger_reason,
            duration_ms=(time.monotonic() - started) * 1000,
            source=source,
        )
        await _dispatch_cleanup_finished(
            hook_runner,
            session=session,
            archive=archive,
            context=context,
            pruned_manager=pruned_manager,
            result=result,
        )
        return result

    # Trigger confirmed and a real cleanup is about to run — dispatch
    # CLEANUP_TRIGGERED BEFORE the (potentially slow) compact/archive LLM
    # call so an observer can tell the user "consolidating memory, please
    # wait". Only the revision-conflict and normal paths reach this point.
    if hook_runner is not None:
        triggered_ctx = MemoryHookContext(
            session_manager=session,
            memory_context=context,
            compression_reason=plan.trigger_reason,
            archive_manager=archive,
            pruned_manager=pruned_manager,
        )
        await hook_runner.dispatch(MemoryHookPoint.CLEANUP_TRIGGERED, triggered_ctx)

    # Phase 2: compact generation
    compact_outcome = await _compact_generation_phase(
        compactor,
        plan.pruned_messages,
        context,
        budget,
    )

    # Phase 3: session commit
    commit_result = await _commit_session_phase(
        session,
        context,
        plan.keep_messages,
        plan.pruned_messages,
        compact_outcome,
        estimator,
    )
    if commit_result is None:
        # Revision conflict.
        # triggered=True, pruned=0; both TRIGGERED (above) and FINISHED dispatch.
        result = CleanupResult(
            triggered=True,
            messages_kept=plan.total_count,
            messages_pruned=0,
            tokens_before=tokens_before,
            tokens_after=tokens_before,
            archive_skipped=True,
            compact_generated=compact_outcome.generated,
            reason=plan.trigger_reason,
            usage=compact_outcome.usage,
            duration_ms=(time.monotonic() - started) * 1000,
            source=source,
        )
        await _dispatch_cleanup_finished(
            hook_runner,
            session=session,
            archive=archive,
            context=context,
            pruned_manager=pruned_manager,
            result=result,
        )
        return result
    keep_count, prune_count, tokens_after = commit_result

    # Phase 4: pruned catalog write
    await _write_pruned_phase(
        pruned_manager,
        plan.pruned_messages,
        context,
        compact_topic=compact_outcome.topic,
    )

    # Phase 5: archive generation (optional, default off)
    archive_outcome = await _generate_archive_phase(
        archive_agent,
        archive_storage,
        archive,
        plan.pruned_messages,
        context,
    )

    # Normal completion.
    # triggered=True, pruned=prune_count; both TRIGGERED (above) and FINISHED dispatch.
    result = CleanupResult(
        triggered=True,
        messages_kept=keep_count,
        messages_pruned=prune_count,
        tokens_before=tokens_before,
        tokens_after=tokens_after,
        archive_skipped=archive_outcome.skipped,
        compact_generated=compact_outcome.generated,
        reason=plan.trigger_reason,
        usage=compact_outcome.usage,
        duration_ms=(time.monotonic() - started) * 1000,
        source=source,
    )
    await _dispatch_cleanup_finished(
        hook_runner,
        session=session,
        archive=archive,
        context=context,
        pruned_manager=pruned_manager,
        result=result,
    )
    return result


_MessageLike = dict[str, Any] | ChatMessage

_SYSTEM_ROLE = str(MessageRole.SYSTEM)


def _estimate_content_tokens(
    messages: Sequence[_MessageLike],
    estimator: TokenEstimator,
) -> int:
    """Estimate persisted message content with the cleanup estimator.

    Parts-list content (persisted ``media://`` references) renders through the
    shared content-part renderer — bracket-line references, never base64.
    """
    total = 0
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            total += estimator.estimate_text(content)
        elif isinstance(content, list):
            rendered = "\n".join(
                (
                    render_content_part_ref(part)
                    if isinstance(part, TextPart | ImageUrlPart)
                    else str(part)
                )
                for part in content
            )
            total += estimator.estimate_text(rendered)
        elif content is not None:
            total += estimator.estimate_text(json.dumps(content, ensure_ascii=False))
    return total


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


def check_cleanup_trigger(
    messages: Sequence[_MessageLike],
    estimator: TokenEstimator,
    max_context_tokens: int | None,
    max_token_ratio: float,
    max_output_tokens: int = 0,
    reserve_tokens: int | None = None,
) -> CompressionReason | None:
    """Fire compression when NON-SYSTEM session tokens exceed threshold.

    ``max_output_tokens`` reserves space for the model's response so the context
    window does not fill to the ratio limit leaving no room to generate.

    Dual-condition trigger (kimi-style, PRD §4.3.2): compression fires when
    EITHER

    - ``pressure > effective_context * max_token_ratio`` (relative), or
    - ``pressure + reserve >= effective_context`` (absolute reserve).

    ``reserve_tokens`` semantics (single definition point of the reserve
    formula):

    - ``None`` (default): auto ``reserve = min(20_000, effective_context // 10)``
      — the absolute condition engages only when it is strictly tighter than
      the relative one;
    - ``0``: the absolute condition is explicitly disabled (legacy
      single-condition behavior for callers that opt out);
    - ``> 0``: caller-declared fixed reserve.
    """
    if max_context_tokens is None:
        return None
    effective_context = max(1, max_context_tokens - max_output_tokens)
    threshold = effective_context * max_token_ratio
    pressure = sum(
        _resolve_message_tokens(m, estimator)
        for m in messages
        if str(m.get("role", "")) != _SYSTEM_ROLE
    )
    if pressure > threshold:
        return CompressionReason.TOKEN_PRESSURE
    reserve = (
        min(20_000, effective_context // 10) if reserve_tokens is None else reserve_tokens
    )
    if 0 < reserve < effective_context and pressure + reserve >= effective_context:
        return CompressionReason.TOKEN_PRESSURE
    return None


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
    boundary, so the whole chain lands in pruned and gets archived.
    """
    while 0 < boundary < len(messages):
        first = messages[boundary]
        if first.get("role") != MessageRole.TOOL:
            break
        tool_call_id = first.get("tool_call_id")
        owner_pruned = any(
            messages[j].get("role") == MessageRole.ASSISTANT
            and any(
                tc.get("id") == tool_call_id
                for tc in (messages[j].get("tool_calls") or [])
            )
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
    """Deep-copy session messages to a timestamped backup before cleanup."""
    try:
        factory = getattr(session, "_storage_factory", None)
        if factory is None:
            return

        bundle = await factory(context)
        store = bundle.messages
        directory: Path | None = getattr(store, "directory", None)
        if directory is None:
            return

        backup_dir = directory / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now(get_user_timezone()).strftime("%Y%m%d_%H%M%S_%f")
        backup_path = backup_dir / f"backup_{timestamp}.jsonl"

        with backup_path.open("w", encoding="utf-8") as handle:
            for msg in messages:
                msg_dict = msg.to_dict() if hasattr(msg, "to_dict") else dict(msg)
                handle.write(json.dumps(msg_dict, ensure_ascii=False) + "\n")

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
        logger.debug("Session backup failed (cleanup will proceed)", exc_info=True)
