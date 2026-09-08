"""Unit tests for the LLMStreamEvent closed union (core/stream_events.py).

Covers discriminator defaults on all six variants, frozen enforcement,
the ``Finish.replay`` round-trip, unknown-kind rejection through the
discriminated union, required-field validation, and TypeAdapter dispatch
proving the union is usable as a standalone type.
"""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from modex_agent.core.llm_struct import FinishReason, LLMErrorInfo, LLMErrorKind, TokenUsage
from modex_agent.core.stream_events import (
    Finish,
    LLMStreamEvent,
    ReasoningDelta,
    ReplayFields,
    StreamFailure,
    TextDelta,
    ToolCallComplete,
    UsageSnapshot,
)

_ADAPTER: TypeAdapter[LLMStreamEvent] = TypeAdapter(LLMStreamEvent)


def test_variant_kind_discriminators() -> None:
    assert TextDelta(text="x").kind == "text_delta"
    assert ReasoningDelta(text="x").kind == "reasoning_delta"
    assert (
        ToolCallComplete(call_id="c1", tool_name="t", arguments={"a": 1}).kind
        == "tool_call_complete"
    )
    assert UsageSnapshot(usage=TokenUsage()).kind == "usage_snapshot"
    assert Finish(finish_reason=FinishReason.STOP).kind == "finish"
    assert (
        StreamFailure(error_info=LLMErrorInfo(kind=LLMErrorKind.TIMEOUT, message="boom")).kind
        == "stream_failure"
    )


def test_frozen_mutation_raises_validation_error() -> None:
    event = TextDelta(text="x")
    with pytest.raises(ValidationError):
        event.text = "y"  # type: ignore[misc]
    replay = ReplayFields(reasoning_signature="s")
    with pytest.raises(ValidationError):
        replay.reasoning_signature = "t"  # type: ignore[misc]


def test_finish_with_replay_roundtrip() -> None:
    finish = Finish(
        finish_reason=FinishReason.STOP,
        replay=ReplayFields(reasoning_signature="s"),
    )
    assert finish.replay is not None
    assert finish.replay.reasoning_signature == "s"
    assert finish.replay.reasoning_content is None
    assert finish.replay.reasoning_item_id is None
    assert finish.replay.reasoning_encrypted_content is None

    data = finish.model_dump()
    assert data["kind"] == "finish"
    assert data["finish_reason"] == "stop"
    assert data["replay"] == {
        "reasoning_content": None,
        "reasoning_signature": "s",
        "reasoning_item_id": None,
        "reasoning_encrypted_content": None,
    }
    assert Finish.model_validate(data) == finish


def test_unknown_kind_rejected_by_union() -> None:
    with pytest.raises(ValidationError):
        _ADAPTER.validate_python({"kind": "mystery", "text": "x"})


def test_text_delta_rejects_none_text() -> None:
    with pytest.raises(ValidationError):
        TextDelta(text=None)  # type: ignore[arg-type]


def test_extra_field_forbidden() -> None:
    with pytest.raises(ValidationError):
        TextDelta(text="x", extra=1)  # type: ignore[call-arg]


def test_union_dispatches_all_six_variants() -> None:
    text = _ADAPTER.validate_python({"kind": "text_delta", "text": "hello"})
    assert isinstance(text, TextDelta)
    assert text.text == "hello"

    reasoning = _ADAPTER.validate_python({"kind": "reasoning_delta", "text": "think"})
    assert isinstance(reasoning, ReasoningDelta)
    assert reasoning.text == "think"

    tool = _ADAPTER.validate_python(
        {
            "kind": "tool_call_complete",
            "call_id": "c1",
            "tool_name": "get_weather",
            "arguments": {"city": "北京"},
        }
    )
    assert isinstance(tool, ToolCallComplete)
    assert tool.call_id == "c1"
    assert tool.tool_name == "get_weather"
    assert tool.arguments == {"city": "北京"}

    usage = _ADAPTER.validate_python({"kind": "usage_snapshot", "usage": {"input_tokens": 3}})
    assert isinstance(usage, UsageSnapshot)
    assert usage.usage.input_tokens == 3

    finish = _ADAPTER.validate_python(
        {
            "kind": "finish",
            "finish_reason": "stop",
            "replay": {"reasoning_item_id": "rs_1"},
        }
    )
    assert isinstance(finish, Finish)
    assert finish.finish_reason is FinishReason.STOP
    assert finish.replay is not None
    assert finish.replay.reasoning_item_id == "rs_1"

    failure = _ADAPTER.validate_python(
        {
            "kind": "stream_failure",
            "error_info": {"kind": "timeout", "message": "boom"},
            "partial_content": "partial",
        }
    )
    assert isinstance(failure, StreamFailure)
    assert failure.error_info.kind is LLMErrorKind.TIMEOUT
    assert failure.partial_content == "partial"


def test_union_accepts_constructed_instances() -> None:
    event = Finish(finish_reason=FinishReason.LENGTH)
    assert _ADAPTER.validate_python(event) == event
