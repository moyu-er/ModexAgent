"""Memory lifecycle telemetry as independent OTel root spans.

The hook subscribes to the memory-owned hook runner, not the ReAct runner.
Each event receives a fresh trace ID and no parent span. Session counters are
kept locally and exposed through :meth:`MemoryTraceHook.read_counters`; score
injection is intentionally deferred to the later cleanup/turn-boundary work.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from enum import StrEnum
from json import dumps
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from modex_agent.memory.hooks import (
    CleanupFinishedHook,
    CleanupTriggeredHook,
    ConsolidationFinishedHook,
    ContextAssembledHook,
    CoreMemoryUpdatedHook,
    LlmUsage,
    MemoryHookContext,
)
from modex_agent.trace.experiment_attrs import ExperimentLinkage, attach_experiment_attrs
from modex_agent.trace.otel_store import OtelSpanTraceStore
from modex_agent.trace.semconv import GenAiAttr, SpanKind
from modex_agent.trace.store import SpanModel

logger = logging.getLogger(__name__)

type _MemoryAttributeValue = str | int | float | bool


class _MemorySpanName(StrEnum):
    CLEANUP_TRIGGERED = "memory.cleanup.triggered"
    CLEANUP_FINISHED = "memory.cleanup.finished"
    CONTEXT_ASSEMBLED = "memory.context.assembled"
    CORE_UPDATED = "memory.core.updated"
    CONSOLIDATION_FINISHED = "memory.consolidation.finished"


class _MemoryAttr(StrEnum):
    SESSION_ID = "memory.session_id"
    TRIGGER = "memory.trigger"
    TS = "memory.ts"
    REASON = "memory.reason"
    MESSAGES_KEPT = "memory.messages_kept"
    MESSAGES_PRUNED = "memory.messages_pruned"
    COMPACT_GENERATED = "memory.compact_generated"
    PRUNE_RATIO = "memory.prune_ratio"
    TOKENS_BEFORE = "memory.tokens_before"
    TOKENS_AFTER = "memory.tokens_after"
    TOKENS_SAVED = "memory.tokens_saved"
    BEFORE_TOKENS = "memory.before_tokens"
    AFTER_TOKENS = "memory.after_tokens"
    MODEL = "memory.model"
    CALLS = "memory.calls"
    INPUT_TOKENS = "memory.input_tokens"
    OUTPUT_TOKENS = "memory.output_tokens"
    CACHE_READ_TOKENS = "memory.cache_read_tokens"
    CACHE_WRITE_TOKENS = "memory.cache_write_tokens"
    DURATION_MS = "memory.duration_ms"
    TRIGGERED = "memory.triggered"
    ARCHIVE_SKIPPED = "memory.archive_skipped"
    AGENT = "memory.agent"
    SECTIONS = "memory.sections"
    FILE = "memory.file"
    UPDATE_MODE = "memory.update.mode"
    UPDATE_TARGET = "memory.update.target"
    UPDATE_CONTENT_DIGEST = "memory.update.content_digest"
    IDEMPOTENT = "memory.idempotent"
    SOURCE_TAG = "memory.source_tag"
    CHANGED = "memory.changed"
    CONSUMED_COUNT = "memory.consumed_count"
    COMPRESSION_RATIO = "memory.compression_ratio"


class MemoryTelemetryCounters(BaseModel):
    """Frozen snapshot of session-level memory event totals."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    memory_cleanup_total: int = 0
    memory_consolidation_total: int = 0
    memory_context_assembled_total: int = 0
    memory_core_updated_total: int = 0


class MemoryTraceHook(
    CleanupTriggeredHook,
    CleanupFinishedHook,
    ContextAssembledHook,
    CoreMemoryUpdatedHook,
    ConsolidationFinishedHook,
):
    """Emit one independent root span for each memory lifecycle event."""

    def __init__(self, store: OtelSpanTraceStore | None) -> None:
        self._store = store
        self._counters: dict[str, MemoryTelemetryCounters] = {}
        self.experiment_linkage: ExperimentLinkage | None = None

    def read_counters(self, session_id: str) -> MemoryTelemetryCounters:
        """Return the current immutable counter snapshot for ``session_id``."""
        return self._counters.get(session_id, MemoryTelemetryCounters())

    async def on_cleanup_triggered(self, ctx: MemoryHookContext) -> None:
        memory_context = ctx.memory_context
        reason = ctx.compression_reason
        if memory_context is None or memory_context.session_id is None or reason is None:
            return
        await self._emit(
            _MemorySpanName.CLEANUP_TRIGGERED,
            memory_context.session_id,
            {_MemoryAttr.TRIGGER: reason.value},
        )

    async def on_cleanup_finished(self, ctx: MemoryHookContext) -> None:
        memory_context = ctx.memory_context
        result = ctx.cleanup_result
        if memory_context is None or memory_context.session_id is None or result is None:
            return
        session_id = memory_context.session_id
        current = self.read_counters(session_id)
        self._counters[session_id] = current.model_copy(
            update={"memory_cleanup_total": current.memory_cleanup_total + 1}
        )
        total_messages = result.messages_kept + result.messages_pruned
        prune_ratio = result.messages_pruned / total_messages if total_messages > 0 else 0.0
        attributes: dict[str, _MemoryAttributeValue] = {
            _MemoryAttr.TS: datetime.now(UTC).isoformat(),
            _MemoryAttr.REASON: result.reason.value if result.reason is not None else "",
            _MemoryAttr.MESSAGES_KEPT: result.messages_kept,
            _MemoryAttr.MESSAGES_PRUNED: result.messages_pruned,
            _MemoryAttr.COMPACT_GENERATED: result.compact_generated,
            _MemoryAttr.PRUNE_RATIO: prune_ratio,
            _MemoryAttr.TOKENS_BEFORE: result.tokens_before,
            _MemoryAttr.TOKENS_AFTER: result.tokens_after,
            _MemoryAttr.TOKENS_SAVED: result.tokens_before - result.tokens_after,
            _MemoryAttr.DURATION_MS: result.duration_ms,
            _MemoryAttr.TRIGGERED: result.triggered,
            _MemoryAttr.ARCHIVE_SKIPPED: result.archive_skipped,
        }
        await self._emit(
            _MemorySpanName.CLEANUP_FINISHED,
            session_id,
            self._with_usage(attributes, result.usage),
        )

    async def on_context_assembled(self, ctx: MemoryHookContext) -> None:
        payload = ctx.context_assembled
        if payload is None:
            return
        current = self.read_counters(payload.session_id)
        self._counters[payload.session_id] = current.model_copy(
            update={
                "memory_context_assembled_total": current.memory_context_assembled_total + 1
            }
        )
        await self._emit(
            _MemorySpanName.CONTEXT_ASSEMBLED,
            payload.session_id,
            {
                _MemoryAttr.AGENT: payload.agent,
                _MemoryAttr.DURATION_MS: payload.duration_ms,
                _MemoryAttr.SECTIONS: dumps(
                    [section.model_dump(mode="json") for section in payload.sections],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            },
        )

    async def on_core_memory_updated(self, ctx: MemoryHookContext) -> None:
        payload = ctx.core_memory_updated
        if payload is None:
            return
        current = self.read_counters(payload.session_id)
        self._counters[payload.session_id] = current.model_copy(
            update={"memory_core_updated_total": current.memory_core_updated_total + 1}
        )
        await self._emit(
            _MemorySpanName.CORE_UPDATED,
            payload.session_id,
            {
                _MemoryAttr.FILE: payload.file,
                _MemoryAttr.UPDATE_MODE: payload.update.mode,
                _MemoryAttr.UPDATE_TARGET: payload.update.target,
                _MemoryAttr.UPDATE_CONTENT_DIGEST: payload.update.content_digest,
                _MemoryAttr.IDEMPOTENT: payload.idempotent,
                _MemoryAttr.SOURCE_TAG: payload.source_tag,
                _MemoryAttr.BEFORE_TOKENS: payload.before_tokens,
                _MemoryAttr.AFTER_TOKENS: payload.after_tokens,
                _MemoryAttr.DURATION_MS: payload.duration_ms,
            },
        )

    async def on_consolidation_finished(self, ctx: MemoryHookContext) -> None:
        payload = ctx.consolidation_finished
        if payload is None:
            return
        current = self.read_counters(payload.session_id)
        self._counters[payload.session_id] = current.model_copy(
            update={
                "memory_consolidation_total": current.memory_consolidation_total + 1
            }
        )
        attributes: dict[str, _MemoryAttributeValue] = {
            _MemoryAttr.TRIGGER: payload.trigger,
            _MemoryAttr.CHANGED: payload.changed,
            _MemoryAttr.CONSUMED_COUNT: payload.consumed_count,
            _MemoryAttr.BEFORE_TOKENS: payload.before_tokens,
            _MemoryAttr.AFTER_TOKENS: payload.after_tokens,
            _MemoryAttr.COMPRESSION_RATIO: payload.compression_ratio,
            _MemoryAttr.DURATION_MS: payload.duration_ms,
        }
        await self._emit(
            _MemorySpanName.CONSOLIDATION_FINISHED,
            payload.session_id,
            self._with_usage(attributes, payload.usage),
        )

    @staticmethod
    def _with_usage(
        attributes: dict[str, _MemoryAttributeValue], usage: LlmUsage | None
    ) -> dict[str, _MemoryAttributeValue]:
        if usage is None:
            return attributes
        return attributes | {
            _MemoryAttr.MODEL: usage.model,
            _MemoryAttr.CALLS: usage.calls,
            _MemoryAttr.INPUT_TOKENS: usage.input_tokens,
            _MemoryAttr.OUTPUT_TOKENS: usage.output_tokens,
            _MemoryAttr.CACHE_READ_TOKENS: usage.cache_read_tokens,
            _MemoryAttr.CACHE_WRITE_TOKENS: usage.cache_write_tokens,
        }

    async def _emit(
        self,
        name: _MemorySpanName,
        session_id: str,
        attributes: dict[str, _MemoryAttributeValue],
    ) -> None:
        if self._store is None:
            return
        timestamp = time.time()
        span = SpanModel(
            trace_id=uuid4().hex,
            span_id=uuid4().hex[:16],
            parent_span_id=None,
            name=name.value,
            kind=SpanKind.INTERNAL,
            start_time=timestamp,
            end_time=timestamp,
            attributes=attributes
            | {
                _MemoryAttr.SESSION_ID: session_id,
                GenAiAttr.CONVERSATION_ID: session_id,
            },
        )
        if self.experiment_linkage is not None:
            span = attach_experiment_attrs(span, self.experiment_linkage)
        try:
            await self._store.save_span(span)
        except Exception:  # noqa: BLE001  # noqa: BROAD_EXCEPT_OK
            logger.warning(
                "Memory trace hook failed to save memory span %s",
                name.value,
                exc_info=True,
            )
