from __future__ import annotations

from datetime import datetime
from json import loads

from modex_agent.core.scope import MemoryContext
from modex_agent.memory.core.models import CompressionReason
from modex_agent.memory.hooks import (
    ConsolidationFinishedPayload,
    ContextAssembledPayload,
    CoreMemoryUpdatedPayload,
    MemoryHookContext,
    MemoryUpdateRef,
    SectionProvenance,
)
from modex_agent.trace.memory_trace_hook import MemoryTraceHook
from tests.unit.trace._memory_trace_support import (
    RecordingStore,
    assert_root_span,
    cleanup_context,
    usage,
)


async def test_cleanup_triggered_emits_one_root_span_with_payload_attributes() -> None:
    store = RecordingStore()
    hook = MemoryTraceHook(store)
    ctx = MemoryHookContext(
        memory_context=MemoryContext(session_id="session-trigger"),
        compression_reason=CompressionReason.MANUAL,
    )

    await hook.on_cleanup_triggered(ctx)

    assert len(store.spans) == 1
    span = store.spans[0]
    assert_root_span(span, "memory.cleanup.triggered", "session-trigger")
    assert span.attributes["memory.trigger"] == "manual"


async def test_cleanup_finished_emits_one_root_span_with_payload_attributes() -> None:
    store = RecordingStore()
    hook = MemoryTraceHook(store)

    await hook.on_cleanup_finished(cleanup_context(usage_value=usage()))

    assert len(store.spans) == 1
    span = store.spans[0]
    assert_root_span(span, "memory.cleanup.finished", "session-a")
    expected = {
        "memory.reason": "token_pressure",
        "memory.messages_kept": 6,
        "memory.messages_pruned": 4,
        "memory.compact_generated": True,
        "memory.prune_ratio": 0.4,
        "memory.tokens_before": 1_000,
        "memory.tokens_after": 650,
        "memory.tokens_saved": 350,
        "memory.model": "memory-model",
        "memory.calls": 2,
        "memory.input_tokens": 101,
        "memory.output_tokens": 37,
        "memory.cache_read_tokens": 11,
        "memory.cache_write_tokens": 7,
        "memory.duration_ms": 12.5,
        "memory.triggered": True,
        "memory.archive_skipped": True,
    }
    for name, value in expected.items():
        assert span.attributes[name] == value
    assert datetime.fromisoformat(span.attributes["memory.ts"])


async def test_context_assembled_emits_one_root_span_with_payload_attributes() -> None:
    store = RecordingStore()
    hook = MemoryTraceHook(store)
    sections = [
        SectionProvenance(
            source="core_memory",
            retrieved_tokens=90,
            injected_tokens=70,
            pruned_tokens=20,
            priority=100,
        )
    ]
    payload = ContextAssembledPayload(
        session_id="session-context",
        agent="main",
        duration_ms=3.25,
        sections=sections,
    )

    await hook.on_context_assembled(MemoryHookContext(context_assembled=payload))

    assert len(store.spans) == 1
    span = store.spans[0]
    assert_root_span(span, "memory.context.assembled", "session-context")
    assert span.attributes["memory.agent"] == "main"
    assert span.attributes["memory.duration_ms"] == 3.25
    assert loads(span.attributes["memory.sections"]) == [sections[0].model_dump(mode="json")]


async def test_core_memory_updated_emits_one_root_span_with_payload_attributes() -> None:
    store = RecordingStore()
    hook = MemoryTraceHook(store)
    payload = CoreMemoryUpdatedPayload(
        session_id="session-core",
        file="MEMORY.md",
        update=MemoryUpdateRef(mode="replace", target="profile", content_digest="abc123"),
        idempotent=False,
        source_tag="dream",
        before_tokens=200,
        after_tokens=240,
        duration_ms=4.5,
    )

    await hook.on_core_memory_updated(MemoryHookContext(core_memory_updated=payload))

    assert len(store.spans) == 1
    span = store.spans[0]
    assert_root_span(span, "memory.core.updated", "session-core")
    assert span.attributes["memory.file"] == "MEMORY.md"
    assert span.attributes["memory.update.mode"] == "replace"
    assert span.attributes["memory.update.target"] == "profile"
    assert span.attributes["memory.update.content_digest"] == "abc123"
    assert span.attributes["memory.idempotent"] is False
    assert span.attributes["memory.source_tag"] == "dream"
    assert span.attributes["memory.before_tokens"] == 200
    assert span.attributes["memory.after_tokens"] == 240
    assert span.attributes["memory.duration_ms"] == 4.5


async def test_consolidation_finished_emits_one_root_span_with_payload_attributes() -> None:
    store = RecordingStore()
    hook = MemoryTraceHook(store)
    payload = ConsolidationFinishedPayload(
        session_id="session-dream",
        trigger="dream",
        changed=True,
        consumed_count=5,
        before_tokens=800,
        after_tokens=500,
        compression_ratio=0.625,
        usage=usage(),
        duration_ms=20.0,
    )

    await hook.on_consolidation_finished(MemoryHookContext(consolidation_finished=payload))

    assert len(store.spans) == 1
    span = store.spans[0]
    assert_root_span(span, "memory.consolidation.finished", "session-dream")
    expected = {
        "memory.trigger": "dream",
        "memory.changed": True,
        "memory.consumed_count": 5,
        "memory.before_tokens": 800,
        "memory.after_tokens": 500,
        "memory.compression_ratio": 0.625,
        "memory.model": "memory-model",
        "memory.calls": 2,
        "memory.input_tokens": 101,
        "memory.output_tokens": 37,
        "memory.cache_read_tokens": 11,
        "memory.cache_write_tokens": 7,
        "memory.duration_ms": 20.0,
    }
    for name, value in expected.items():
        assert span.attributes[name] == value


async def test_cleanup_metric_record_fields_have_complete_span_attribute_parity() -> None:
    store = RecordingStore()
    hook = MemoryTraceHook(store)

    await hook.on_cleanup_finished(cleanup_context(usage_value=usage()))

    attrs = store.spans[0].attributes
    field_attributes = {
        "ts": {"memory.ts"},
        "session_id": {"memory.session_id"},
        "reason": {"memory.reason"},
        "messages_kept": {"memory.messages_kept"},
        "messages_pruned": {"memory.messages_pruned"},
        "compact_generated": {"memory.compact_generated"},
        "prune_ratio": {"memory.prune_ratio"},
        "tokens_before": {"memory.tokens_before"},
        "tokens_after": {"memory.tokens_after"},
        "tokens_saved": {"memory.tokens_saved"},
        "usage": {
            "memory.model",
            "memory.calls",
            "memory.input_tokens",
            "memory.output_tokens",
            "memory.cache_read_tokens",
            "memory.cache_write_tokens",
        },
        "duration_ms": {"memory.duration_ms"},
    }
    assert set(field_attributes) == {
        "ts",
        "session_id",
        "reason",
        "messages_kept",
        "messages_pruned",
        "compact_generated",
        "prune_ratio",
        "tokens_before",
        "tokens_after",
        "tokens_saved",
        "usage",
        "duration_ms",
    }
    for attribute_names in field_attributes.values():
        assert attribute_names <= attrs.keys()
