from __future__ import annotations

import pytest
from pydantic import ValidationError

from modex_agent.memory import hooks


class _ExpectedHookError(RuntimeError):
    pass


def _usage() -> hooks.LlmUsage:
    return hooks.LlmUsage(
        model="test-model",
        calls=2,
        input_tokens=100,
        output_tokens=25,
        cache_read_tokens=10,
        cache_write_tokens=5,
    )


def _section() -> hooks.SectionProvenance:
    return hooks.SectionProvenance(
        source="core_memory",
        retrieved_tokens=40,
        injected_tokens=30,
        pruned_tokens=10,
        priority=100,
    )


async def test_context_assembled_dispatch_carries_payload_values() -> None:
    payload = hooks.ContextAssembledPayload(
        session_id="session-context",
        agent="main",
        duration_ms=12.5,
        sections=[_section()],
    )

    class RecordingHook(hooks.ContextAssembledHook):
        def __init__(self) -> None:
            self.received: hooks.ContextAssembledPayload | None = None

        async def on_context_assembled(self, ctx: hooks.MemoryHookContext) -> None:
            self.received = ctx.context_assembled

    recording = RecordingHook()
    runner = hooks.MemoryHookRunner()
    runner.add(recording)

    await runner.dispatch(
        hooks.MemoryHookPoint.CONTEXT_ASSEMBLED,
        hooks.MemoryHookContext(context_assembled=payload),
    )

    received = recording.received
    assert received is not None
    assert received is payload
    assert received.session_id == "session-context"
    assert received.agent == "main"
    assert received.duration_ms == 12.5
    assert received.sections == [_section()]


async def test_core_memory_updated_dispatch_carries_payload_values() -> None:
    update = hooks.MemoryUpdateRef(
        mode="append",
        target="MEMORY.md",
        content_digest="sha256:1234abcd",
    )
    payload = hooks.CoreMemoryUpdatedPayload(
        session_id="session-update",
        file="MEMORY.md",
        update=update,
        idempotent=True,
        source_tag="agent_tool",
        before_tokens=20,
        after_tokens=27,
        duration_ms=4.25,
    )

    class RecordingHook(hooks.CoreMemoryUpdatedHook):
        def __init__(self) -> None:
            self.received: hooks.CoreMemoryUpdatedPayload | None = None

        async def on_core_memory_updated(self, ctx: hooks.MemoryHookContext) -> None:
            self.received = ctx.core_memory_updated

    recording = RecordingHook()
    runner = hooks.MemoryHookRunner()
    runner.add(recording)

    await runner.dispatch(
        hooks.MemoryHookPoint.CORE_MEMORY_UPDATED,
        hooks.MemoryHookContext(core_memory_updated=payload),
    )

    received = recording.received
    assert received is not None
    assert received is payload
    assert received.session_id == "session-update"
    assert received.file == "MEMORY.md"
    assert received.update == update
    assert received.idempotent is True
    assert received.source_tag == "agent_tool"
    assert received.before_tokens == 20
    assert received.after_tokens == 27
    assert received.duration_ms == 4.25


async def test_consolidation_finished_dispatch_carries_payload_values() -> None:
    payload = hooks.ConsolidationFinishedPayload(
        session_id="session-consolidation",
        trigger="dream",
        changed=True,
        consumed_count=3,
        before_tokens=80,
        after_tokens=50,
        compression_ratio=0.625,
        usage=_usage(),
        duration_ms=18.0,
    )

    class RecordingHook(hooks.ConsolidationFinishedHook):
        def __init__(self) -> None:
            self.received: hooks.ConsolidationFinishedPayload | None = None

        async def on_consolidation_finished(self, ctx: hooks.MemoryHookContext) -> None:
            self.received = ctx.consolidation_finished

    recording = RecordingHook()
    runner = hooks.MemoryHookRunner()
    runner.add(recording)

    await runner.dispatch(
        hooks.MemoryHookPoint.CONSOLIDATION_FINISHED,
        hooks.MemoryHookContext(consolidation_finished=payload),
    )

    received = recording.received
    assert received is not None
    assert received is payload
    assert received.session_id == "session-consolidation"
    assert received.trigger == "dream"
    assert received.changed is True
    assert received.consumed_count == 3
    assert received.before_tokens == 80
    assert received.after_tokens == 50
    assert received.compression_ratio == 0.625
    assert received.usage == _usage()
    assert received.duration_ms == 18.0


async def test_new_point_failure_is_logged_and_next_hook_receives_payload(
    caplog: pytest.LogCaptureFixture,
) -> None:
    payload = hooks.CoreMemoryUpdatedPayload(
        session_id="session-isolation",
        file="USER.md",
        update=hooks.MemoryUpdateRef(
            mode="section_replace",
            target="preferences",
            content_digest="sha256:deadbeef",
        ),
        idempotent=False,
        source_tag="consolidator",
        before_tokens=12,
        after_tokens=10,
        duration_ms=2.0,
    )

    class RaisingHook(hooks.CoreMemoryUpdatedHook):
        async def on_core_memory_updated(self, ctx: hooks.MemoryHookContext) -> None:
            raise _ExpectedHookError

    class RecordingHook(hooks.CoreMemoryUpdatedHook):
        def __init__(self) -> None:
            self.received: hooks.CoreMemoryUpdatedPayload | None = None

        async def on_core_memory_updated(self, ctx: hooks.MemoryHookContext) -> None:
            self.received = ctx.core_memory_updated

    recording = RecordingHook()
    runner = hooks.MemoryHookRunner()
    runner.add(RaisingHook())
    runner.add(recording)

    with caplog.at_level("WARNING", logger="modex_agent.memory.hooks"):
        await runner.dispatch(
            hooks.MemoryHookPoint.CORE_MEMORY_UPDATED,
            hooks.MemoryHookContext(core_memory_updated=payload),
        )

    assert recording.received is payload
    assert any("failed at core_memory_updated" in record.message for record in caplog.records)


def test_payload_models_have_exact_ticket_fields() -> None:
    expected_fields = {
        hooks.LlmUsage: {
            "model",
            "calls",
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
        },
        hooks.SectionProvenance: {
            "source",
            "retrieved_tokens",
            "injected_tokens",
            "pruned_tokens",
            "priority",
        },
        hooks.ContextAssembledPayload: {"session_id", "agent", "duration_ms", "sections"},
        hooks.MemoryUpdateRef: {"mode", "target", "content_digest"},
        hooks.CoreMemoryUpdatedPayload: {
            "session_id",
            "file",
            "update",
            "idempotent",
            "source_tag",
            "before_tokens",
            "after_tokens",
            "duration_ms",
        },
        hooks.ConsolidationFinishedPayload: {
            "session_id",
            "trigger",
            "changed",
            "consumed_count",
            "before_tokens",
            "after_tokens",
            "compression_ratio",
            "usage",
            "duration_ms",
        },
    }

    for model, fields in expected_fields.items():
        assert set(model.model_fields) == fields


def test_payload_models_reject_missing_and_extra_fields() -> None:
    with pytest.raises(ValidationError):
        hooks.LlmUsage.model_validate(
            {
                "model": "test-model",
                "calls": 1,
                "input_tokens": 2,
                "output_tokens": 3,
                "cache_read_tokens": 4,
            }
        )

    with pytest.raises(ValidationError):
        hooks.SectionProvenance.model_validate(
            {
                "source": "core_memory",
                "retrieved_tokens": 4,
                "injected_tokens": 3,
                "pruned_tokens": 1,
                "priority": 100,
                "unexpected": True,
            }
        )
