from __future__ import annotations

import pytest
from pydantic import ValidationError

from modex_agent.core.turn_events import (
    TurnReasoningEvent,
    TurnTextEvent,
    TurnToolCallEvent,
    TurnToolResultEvent,
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
