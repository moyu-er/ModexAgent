from __future__ import annotations

from typing import get_args

import pytest
from pydantic import TypeAdapter, ValidationError

from modex_agent.core.llm_struct import TokenUsage
from modex_agent.core.turn_events import (
    ApprovalRequestedEvent,
    ApprovalResolvedEvent,
    IterationFinishedEvent,
    IterationStartedEvent,
    ProgressEvent,
    StopReason,
    ToolArgsDeltaEvent,
    TurnErroredEvent,
    TurnEvent,
    TurnFinishedEvent,
    TurnReasoningEvent,
    TurnStartedEvent,
    TurnTextEvent,
    TurnToolCallEvent,
    TurnToolResultEvent,
    UsageEvent,
)


def test_turn_events_are_frozen_and_forbid_extra_fields() -> None:
    event = TurnTextEvent(text="hello")

    with pytest.raises(ValidationError):
        event.text = "changed"
    with pytest.raises(ValidationError):
        TurnTextEvent.model_validate({"kind": "text", "text": "hello", "extra": 1})


def test_turn_events_reject_coercion_and_missing_tool_identity() -> None:
    with pytest.raises(ValidationError):
        TurnReasoningEvent.model_validate({"kind": "reasoning", "text": 1})
    with pytest.raises(ValidationError):
        TurnToolCallEvent(tool_name="bash", call_id="", arguments={})
    with pytest.raises(ValidationError):
        TurnToolResultEvent(call_id="", tool_name="bash", output="ok")


def test_tool_events_accept_nested_json_arguments() -> None:
    event = TurnToolCallEvent(
        tool_name="bash",
        call_id="call-1",
        arguments={"command": "ls", "options": {"hidden": False}, "limit": 2},
    )

    assert event.arguments["options"] == {"hidden": False}


# ── Unified runtime vocabulary (ADR-0054) ───────────────────────────────────

_ADAPTER = TypeAdapter(TurnEvent)

_NEW_KIND_SAMPLES: list[TurnEvent] = [
    TurnStartedEvent(),
    TurnFinishedEvent(stop_reason=StopReason.COMPLETED, attachments=("/tmp/a.png",)),
    TurnFinishedEvent(stop_reason=StopReason.ERROR, error="boom"),
    TurnErroredEvent(message="mid-turn failure"),
    ToolArgsDeltaEvent(call_id="c1", tool_name="bash", args_fragment='{"cmd'),
    ToolArgsDeltaEvent(call_id="c1", tool_name="bash", args_fragment=""),
    ApprovalRequestedEvent(tool_name="bash", call_id="c1", prompt="allow?"),
    ApprovalResolvedEvent(call_id="c1", approved=False),
    UsageEvent(usage=TokenUsage(prompt_tokens=3, completion_tokens=5)),
    IterationStartedEvent(iteration=0),
    IterationFinishedEvent(iteration=0, has_tool_calls=True),
    ProgressEvent(hint="settling tools", tool_hint=True),
]


def test_every_variant_is_in_the_closed_union() -> None:
    variants = set(get_args(get_args(TurnEvent)[0]))
    expected = {
        TurnTextEvent,
        TurnReasoningEvent,
        TurnToolCallEvent,
        TurnToolResultEvent,
        TurnStartedEvent,
        TurnFinishedEvent,
        TurnErroredEvent,
        ToolArgsDeltaEvent,
        ApprovalRequestedEvent,
        ApprovalResolvedEvent,
        UsageEvent,
        IterationStartedEvent,
        IterationFinishedEvent,
        ProgressEvent,
    }
    assert variants == expected


@pytest.mark.parametrize("event", _NEW_KIND_SAMPLES, ids=lambda e: e.kind)
def test_new_variants_round_trip_through_the_discriminated_union(
    event: TurnEvent,
) -> None:
    reloaded = _ADAPTER.validate_json(event.model_dump_json())
    assert reloaded == event
    assert reloaded.kind == event.kind


@pytest.mark.parametrize("event", _NEW_KIND_SAMPLES, ids=lambda e: e.kind)
def test_new_variants_are_frozen_and_forbid_extra_fields(event: TurnEvent) -> None:
    with pytest.raises(ValidationError):
        event.kind = "text"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        type(event).model_validate({**event.model_dump(), "extra": 1})


def test_new_variant_id_fields_reject_empty_strings() -> None:
    with pytest.raises(ValidationError):
        ToolArgsDeltaEvent(call_id="", tool_name="bash", args_fragment="")
    with pytest.raises(ValidationError):
        ApprovalRequestedEvent(tool_name="", call_id="c1", prompt="allow?")
    with pytest.raises(ValidationError):
        ApprovalResolvedEvent(call_id="", approved=True)
