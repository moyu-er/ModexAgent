"""Unit tests for the LLMRequest sampling envelope.

Covers construction, frozen immutability, ``model_dump()`` round-trips
(python and JSON modes, with nested ChatMessage / tools tuple /
reasoning_effort enum), ``extra="forbid"`` rejection, defaults, and the
empty-messages contract.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from modex_agent.core.llm_request import LLMRequest, ReasoningEffort
from modex_agent.core.message import ChatMessage, ToolCall


def _full_request() -> LLMRequest:
    return LLMRequest(
        model="deepseek-chat",
        messages=[
            ChatMessage(role="user", content="hello"),
            ChatMessage(
                role="assistant",
                content=None,
                tool_calls=[ToolCall(tool_name="get_weather", arguments={"city": "Tokyo"}, call_id="call_1")],
            ),
            ChatMessage(role="tool", content="sunny", tool_call_id="call_1", name="get_weather"),
        ],
        tools=({"type": "function", "function": {"name": "get_weather", "parameters": {"type": "object"}}},),
        temperature=0.2,
        top_p=0.9,
        max_output_tokens=4096,
        stop=("\n\n",),
        reasoning_effort=ReasoningEffort.HIGH,
        prompt_cache_key="session-42",
        extra_body={"thinking": {"type": "enabled", "budget_tokens": 4096}},
    )


# ---------------------------------------------------------------------------
# Construction and immutability
# ---------------------------------------------------------------------------


def test_construction_full_field_surface() -> None:
    request = _full_request()
    assert request.model == "deepseek-chat"
    assert len(request.messages) == 3
    assert request.messages[1].tool_calls is not None
    assert request.tools == (
        {"type": "function", "function": {"name": "get_weather", "parameters": {"type": "object"}}},
    )
    assert request.temperature == 0.2
    assert request.top_p == 0.9
    assert request.max_output_tokens == 4096
    assert request.stop == ("\n\n",)
    assert request.reasoning_effort is ReasoningEffort.HIGH
    assert request.prompt_cache_key == "session-42"
    assert request.extra_body == {"thinking": {"type": "enabled", "budget_tokens": 4096}}


def test_frozen_rejects_mutation() -> None:
    request = _full_request()
    with pytest.raises(ValidationError):
        request.model = "other-model"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        request.temperature = 0.5  # type: ignore[misc]


def test_tools_and_stop_lists_are_coerced_to_tuples() -> None:
    request = LLMRequest(
        model="m",
        messages=[],
        tools=[{"type": "function", "function": {"name": "f"}}],
        stop=["END"],
    )
    assert isinstance(request.tools, tuple)
    assert isinstance(request.stop, tuple)


def test_reasoning_effort_accepts_wire_string() -> None:
    request = LLMRequest(model="m", messages=[], reasoning_effort="high")
    assert request.reasoning_effort is ReasoningEffort.HIGH


# ---------------------------------------------------------------------------
# Round-trips
# ---------------------------------------------------------------------------


def test_model_dump_round_trip_python_mode() -> None:
    request = _full_request()
    assert LLMRequest.model_validate(request.model_dump()) == request


def test_model_dump_round_trip_json_mode() -> None:
    request = _full_request()
    assert LLMRequest.model_validate(request.model_dump(mode="json")) == request


def test_round_trip_preserves_nested_chatmessage_and_enum() -> None:
    request = _full_request()
    restored = LLMRequest.model_validate(request.model_dump(mode="json"))
    assert isinstance(restored.messages[0], ChatMessage)
    assert restored.messages[0].content == "hello"
    assert restored.messages[1].tool_calls == [ToolCall(tool_name="get_weather", arguments={"city": "Tokyo"}, call_id="call_1")]
    assert restored.reasoning_effort is ReasoningEffort.HIGH
    assert restored.tools[0]["function"]["name"] == "get_weather"
    assert restored.extra_body is not None and "thinking" in restored.extra_body


# ---------------------------------------------------------------------------
# extra="forbid" and defaults
# ---------------------------------------------------------------------------


def test_unknown_field_rejected() -> None:
    with pytest.raises(ValidationError, match="bogus"):
        LLMRequest(model="m", messages=[], bogus=1)  # type: ignore[call-arg]


def test_defaults() -> None:
    request = LLMRequest(model="m", messages=[])
    assert request.model == "m"
    assert request.messages == []
    assert request.tools == ()
    assert request.temperature is None
    assert request.top_p is None
    assert request.max_output_tokens is None
    assert request.stop is None
    assert request.reasoning_effort is ReasoningEffort.NONE
    assert request.prompt_cache_key is None
    assert request.extra_body is None


def test_empty_messages_allowed() -> None:
    request = LLMRequest(model="m", messages=[])
    assert request.messages == []


def test_extra_body_accepts_arbitrary_payload() -> None:
    payload: dict[str, Any] = {"vendor_option": [1, {"nested": True}]}
    request = LLMRequest(model="m", messages=[], extra_body=payload)
    assert request.extra_body == payload
