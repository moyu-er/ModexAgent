from __future__ import annotations

from modex_agent.memory.cleanup import CleanupResult
from modex_agent.memory.core.models import CompressionReason
from modex_agent.memory.hooks import LlmUsage, MemoryHookContext
from modex_agent.memory.scope import MemoryContext
from modex_agent.trace.otel_store import OtelSpanTraceStore
from modex_agent.trace.semconv import GenAiAttr, SpanKind, SpanStatusCode
from modex_agent.trace.store import SpanModel


class StoreWriteError(Exception):
    pass


class RecordingStore(OtelSpanTraceStore):
    def __init__(self, *, fail: bool = False) -> None:
        self.spans: list[SpanModel] = []
        self._fail = fail

    async def save_span(self, span: SpanModel) -> None:
        if self._fail:
            raise StoreWriteError
        self.spans.append(span)


def usage() -> LlmUsage:
    return LlmUsage(
        model="memory-model",
        calls=2,
        input_tokens=101,
        output_tokens=37,
        cache_read_tokens=11,
        cache_write_tokens=7,
    )


def cleanup_context(
    session_id: str = "session-a", *, usage_value: LlmUsage | None = None
) -> MemoryHookContext:
    return MemoryHookContext(
        memory_context=MemoryContext(session_id=session_id),
        cleanup_result=CleanupResult(
            triggered=True,
            messages_kept=6,
            messages_pruned=4,
            tokens_before=1_000,
            tokens_after=650,
            archive_skipped=True,
            compact_generated=True,
            reason=CompressionReason.TOKEN_PRESSURE,
            usage=usage_value,
            duration_ms=12.5,
        ),
        compression_reason=CompressionReason.TOKEN_PRESSURE,
    )


def assert_root_span(span: SpanModel, name: str, session_id: str) -> None:
    assert span.name == name
    assert span.kind == SpanKind.INTERNAL.value
    assert span.parent_span_id is None
    assert len(span.trace_id) == 32
    assert len(span.span_id) == 16
    assert span.start_time == span.end_time
    assert span.status.code is SpanStatusCode.OK
    assert span.attributes[GenAiAttr.CONVERSATION_ID] == session_id
    assert span.attributes["memory.session_id"] == session_id
