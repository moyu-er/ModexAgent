from __future__ import annotations

import pytest

from modex_agent.memory.hooks import (
    ConsolidationFinishedPayload,
    ContextAssembledPayload,
    CoreMemoryUpdatedPayload,
    MemoryHookContext,
    MemoryUpdateRef,
)
from modex_agent.trace.memory_trace_hook import MemoryTelemetryCounters, MemoryTraceHook
from tests.unit.trace._memory_trace_support import RecordingStore, cleanup_context


async def test_none_usage_omits_usage_attributes_instead_of_zero_filling() -> None:
    store = RecordingStore()
    hook = MemoryTraceHook(store)

    await hook.on_cleanup_finished(cleanup_context())

    assert not {
        "memory.model",
        "memory.calls",
        "memory.input_tokens",
        "memory.output_tokens",
        "memory.cache_read_tokens",
        "memory.cache_write_tokens",
    } & store.spans[0].attributes.keys()


async def test_store_failure_is_logged_and_does_not_escape(
    caplog: pytest.LogCaptureFixture,
) -> None:
    hook = MemoryTraceHook(RecordingStore(fail=True))

    with caplog.at_level("WARNING", logger="modex_agent.trace.memory_trace_hook"):
        await hook.on_cleanup_finished(cleanup_context())

    assert "failed to save memory span" in caplog.text.lower()


async def test_none_store_emits_nothing_and_does_not_crash() -> None:
    hook = MemoryTraceHook(None)

    await hook.on_cleanup_finished(cleanup_context())

    assert hook.read_counters("session-a") == MemoryTelemetryCounters(
        memory_cleanup_total=1
    )


async def test_counters_accumulate_and_are_isolated_per_session() -> None:
    hook = MemoryTraceHook(RecordingStore())
    context_payload = ContextAssembledPayload(
        session_id="session-a", agent="main", duration_ms=1.0, sections=[]
    )
    core_payload = CoreMemoryUpdatedPayload(
        session_id="session-a",
        file="MEMORY.md",
        update=MemoryUpdateRef(mode="append", target="facts", content_digest="def456"),
        idempotent=True,
        source_tag="tool",
        before_tokens=1,
        after_tokens=2,
        duration_ms=1.0,
    )
    consolidation_payload = ConsolidationFinishedPayload(
        session_id="session-b",
        trigger="core",
        changed=False,
        consumed_count=0,
        before_tokens=2,
        after_tokens=2,
        compression_ratio=1.0,
        usage=None,
        duration_ms=1.0,
    )

    await hook.on_cleanup_finished(cleanup_context())
    await hook.on_cleanup_finished(cleanup_context())
    await hook.on_context_assembled(MemoryHookContext(context_assembled=context_payload))
    await hook.on_core_memory_updated(MemoryHookContext(core_memory_updated=core_payload))
    await hook.on_consolidation_finished(
        MemoryHookContext(consolidation_finished=consolidation_payload)
    )

    assert hook.read_counters("session-a") == MemoryTelemetryCounters(
        memory_cleanup_total=2,
        memory_context_assembled_total=1,
        memory_core_updated_total=1,
    )
    assert hook.read_counters("session-b") == MemoryTelemetryCounters(
        memory_consolidation_total=1
    )
    assert hook.read_counters("missing") == MemoryTelemetryCounters()
