"""Unit tests for ``OpenCodeV2EventParser`` — SSE line → core TurnEvent table.

The V2 protocol surface (``/api/event``) uses the envelope
``{id, type, data, metadata?, durable?, location?}`` with the payload in
``data`` (NOT ``properties`` like V1). Heartbeats are SSE comment lines
(``: heartbeat``), not typed events.

These tests cover every line → ``TurnEvent`` mapping in the spec, the
provider-limit behaviors (tool-name association, JSON-parse-failure
argument synthesis), heartbeat stripping, V1-event handling, and
malformed input. Child-session routing is NOT a parser concern (the
SSE reader's callback demux owns it — see ``test_opencode_v2_sse_reader.py``).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import pytest

from modex_agent.agents.external.providers.opencode.v2_parser import (
    OpenCodeV2EventParser,
    OpenCodeV2EventType,
)
from modex_agent.core.turn_events import (
    TurnErroredEvent,
    TurnEvent,
    TurnReasoningEvent,
    TurnTextEvent,
    TurnToolCallEvent,
    TurnToolResultEvent,
)


def _sse_line(payload: Mapping[str, object]) -> str:
    return json.dumps(payload)


def _events(parser: OpenCodeV2EventParser, *payloads: Mapping[str, object]) -> list[TurnEvent]:
    out: list[TurnEvent] = []
    for p in payloads:
        out.extend(parser.parse_line(_sse_line(p)))
    return out


def _v2_event(
    event_type: str,
    data: Mapping[str, Any] | None = None,
    *,
    event_id: str = "evt_1",
) -> dict[str, Any]:
    return {"id": event_id, "type": event_type, "data": dict(data) if data else {}}


def _tool_called(parser: OpenCodeV2EventParser, call_id: str = "call_1", tool: str = "read") -> None:
    """Feed a preceding ``tool.called`` so result events can resolve the name."""
    _events(
        parser,
        _v2_event(
            OpenCodeV2EventType.SESSION_NEXT_TOOL_CALLED,
            {"sessionID": "ses_1", "callID": call_id, "tool": tool, "input": {}},
        ),
    )


# ---------------------------------------------------------------------------
# Text / reasoning deltas
# ---------------------------------------------------------------------------


class TestOpenCodeV2TextDelta:
    def test_text_delta_yields_turn_text_event(self) -> None:
        parser = OpenCodeV2EventParser()
        out = _events(
            parser,
            _v2_event(
                OpenCodeV2EventType.SESSION_NEXT_TEXT_DELTA,
                {
                    "sessionID": "ses_1",
                    "assistantMessageID": "m1",
                    "textID": "t1",
                    "delta": "Hello",
                },
            ),
        )
        assert out == [TurnTextEvent(text="Hello")]

    def test_text_delta_multiple_fragments_preserve_order(self) -> None:
        parser = OpenCodeV2EventParser()
        out = _events(
            parser,
            _v2_event(
                OpenCodeV2EventType.SESSION_NEXT_TEXT_DELTA,
                {"sessionID": "s", "delta": "Hello "},
                event_id="e1",
            ),
            _v2_event(
                OpenCodeV2EventType.SESSION_NEXT_TEXT_DELTA,
                {"sessionID": "s", "delta": "world!"},
                event_id="e2",
            ),
        )
        assert out == [TurnTextEvent(text="Hello "), TurnTextEvent(text="world!")]

    def test_text_delta_empty_string_yields_nothing(self) -> None:
        parser = OpenCodeV2EventParser()
        out = _events(
            parser,
            _v2_event(
                OpenCodeV2EventType.SESSION_NEXT_TEXT_DELTA,
                {"sessionID": "s", "delta": ""},
            ),
        )
        assert out == []

    def test_text_delta_missing_delta_yields_nothing(self) -> None:
        parser = OpenCodeV2EventParser()
        out = _events(
            parser,
            _v2_event(
                OpenCodeV2EventType.SESSION_NEXT_TEXT_DELTA,
                {"sessionID": "s"},
            ),
        )
        assert out == []


class TestOpenCodeV2ReasoningDelta:
    def test_reasoning_delta_yields_turn_reasoning_event(self) -> None:
        parser = OpenCodeV2EventParser()
        out = _events(
            parser,
            _v2_event(
                OpenCodeV2EventType.SESSION_NEXT_REASONING_DELTA,
                {
                    "sessionID": "ses_1",
                    "assistantMessageID": "m1",
                    "reasoningID": "r1",
                    "delta": "Thinking...",
                },
            ),
        )
        assert out == [TurnReasoningEvent(text="Thinking...")]


# ---------------------------------------------------------------------------
# Tool events
# ---------------------------------------------------------------------------


class TestOpenCodeV2ToolCalled:
    def test_tool_called_yields_turn_tool_call_event(self) -> None:
        parser = OpenCodeV2EventParser()
        out = _events(
            parser,
            _v2_event(
                OpenCodeV2EventType.SESSION_NEXT_TOOL_CALLED,
                {
                    "sessionID": "ses_1",
                    "assistantMessageID": "m1",
                    "callID": "call_1",
                    "tool": "read",
                    "input": {"path": "/tmp/foo.py"},
                    "provider": {"executed": False},
                },
            ),
        )
        assert out == [
            TurnToolCallEvent(
                tool_name="read", call_id="call_1", arguments={"path": "/tmp/foo.py"}
            )
        ]

    def test_tool_called_with_string_input(self) -> None:
        parser = OpenCodeV2EventParser()
        out = _events(
            parser,
            _v2_event(
                OpenCodeV2EventType.SESSION_NEXT_TOOL_CALLED,
                {
                    "sessionID": "ses_1",
                    "assistantMessageID": "m1",
                    "callID": "call_2",
                    "tool": "write",
                    "input": "raw string input",
                    "provider": {"executed": False},
                },
            ),
        )
        assert len(out) == 1
        assert out[0].arguments == {"input": "raw string input"}

    def test_tool_called_with_non_object_json_input_synthesizes_input_key(self) -> None:
        """Provider limit: models that stream free-form (non-object JSON)
        arguments get their payload synthesized as ``{"input": <raw>}``."""
        parser = OpenCodeV2EventParser()
        out = _events(
            parser,
            _v2_event(
                OpenCodeV2EventType.SESSION_NEXT_TOOL_CALLED,
                {
                    "sessionID": "ses_1",
                    "callID": "call_3",
                    "tool": "bash",
                    "input": "[not an object",
                },
            ),
        )
        assert out == [
            TurnToolCallEvent(
                tool_name="bash", call_id="call_3", arguments={"input": "[not an object"}
            )
        ]

    def test_tool_called_missing_tool_name_yields_nothing(self) -> None:
        parser = OpenCodeV2EventParser()
        out = _events(
            parser,
            _v2_event(
                OpenCodeV2EventType.SESSION_NEXT_TOOL_CALLED,
                {
                    "sessionID": "ses_1",
                    "callID": "call_1",
                    "input": {},
                    "provider": {"executed": False},
                },
            ),
        )
        assert out == []

    def test_tool_called_missing_call_id_yields_nothing(self) -> None:
        parser = OpenCodeV2EventParser()
        out = _events(
            parser,
            _v2_event(
                OpenCodeV2EventType.SESSION_NEXT_TOOL_CALLED,
                {
                    "sessionID": "ses_1",
                    "tool": "read",
                    "input": {},
                },
            ),
        )
        assert out == []


class TestOpenCodeV2ToolSuccess:
    def test_tool_success_with_text_content(self) -> None:
        parser = OpenCodeV2EventParser()
        _tool_called(parser, call_id="call_1", tool="read")
        out = _events(
            parser,
            _v2_event(
                OpenCodeV2EventType.SESSION_NEXT_TOOL_SUCCESS,
                {
                    "sessionID": "ses_1",
                    "assistantMessageID": "m1",
                    "callID": "call_1",
                    "structured": {},
                    "content": [{"type": "text", "text": "file contents here"}],
                    "provider": {"executed": True},
                },
            ),
        )
        assert len(out) == 1
        assert isinstance(out[0], TurnToolResultEvent)
        assert out[0].tool_name == "read"
        assert out[0].call_id == "call_1"
        assert out[0].output == "file contents here"
        assert out[0].error is None

    def test_tool_success_with_multiple_text_content_joined(self) -> None:
        parser = OpenCodeV2EventParser()
        _tool_called(parser, call_id="call_1", tool="read")
        out = _events(
            parser,
            _v2_event(
                OpenCodeV2EventType.SESSION_NEXT_TOOL_SUCCESS,
                {
                    "sessionID": "ses_1",
                    "callID": "call_1",
                    "structured": {},
                    "content": [
                        {"type": "text", "text": "line1\n"},
                        {"type": "text", "text": "line2\n"},
                    ],
                },
            ),
        )
        assert len(out) == 1
        assert out[0].output == "line1\nline2\n"

    def test_tool_success_with_structured_when_no_content(self) -> None:
        parser = OpenCodeV2EventParser()
        _tool_called(parser, call_id="call_1", tool="read")
        out = _events(
            parser,
            _v2_event(
                OpenCodeV2EventType.SESSION_NEXT_TOOL_SUCCESS,
                {
                    "sessionID": "ses_1",
                    "callID": "call_1",
                    "structured": {"rows": 42, "matched": 3},
                    "content": [],
                },
            ),
        )
        assert len(out) == 1
        assert json.loads(out[0].output) == {"rows": 42, "matched": 3}

    def test_tool_success_empty_content_and_structured_yields_nothing(self) -> None:
        parser = OpenCodeV2EventParser()
        _tool_called(parser, call_id="call_1", tool="read")
        out = _events(
            parser,
            _v2_event(
                OpenCodeV2EventType.SESSION_NEXT_TOOL_SUCCESS,
                {
                    "sessionID": "ses_1",
                    "callID": "call_1",
                    "structured": {},
                    "content": [],
                    "provider": {"executed": True},
                },
            ),
        )
        assert out == []


class TestOpenCodeV2ToolFailed:
    def test_tool_failed_yields_result_with_error_filled(self) -> None:
        parser = OpenCodeV2EventParser()
        _tool_called(parser, call_id="call_1", tool="read")
        out = _events(
            parser,
            _v2_event(
                OpenCodeV2EventType.SESSION_NEXT_TOOL_FAILED,
                {
                    "sessionID": "ses_1",
                    "callID": "call_1",
                    "error": {"type": "unknown", "message": "File not found"},
                    "provider": {"executed": True},
                },
            ),
        )
        assert len(out) == 1
        assert isinstance(out[0], TurnToolResultEvent)
        assert out[0].tool_name == "read"
        assert out[0].call_id == "call_1"
        assert out[0].output == "File not found"
        assert out[0].error == "File not found"

    def test_tool_failed_with_string_error(self) -> None:
        parser = OpenCodeV2EventParser()
        _tool_called(parser, call_id="call_1", tool="bash")
        out = _events(
            parser,
            _v2_event(
                OpenCodeV2EventType.SESSION_NEXT_TOOL_FAILED,
                {
                    "sessionID": "ses_1",
                    "callID": "call_1",
                    "error": "Network error",
                },
            ),
        )
        assert len(out) == 1
        assert out[0].output == "Network error"
        assert out[0].error == "Network error"


class TestToolNameAssociation:
    """V2 success/failed events carry only ``callID`` — the tool name is
    remembered from the preceding ``tool.called`` and consumed on first use."""

    def test_result_without_preceding_call_is_dropped(self) -> None:
        parser = OpenCodeV2EventParser()
        out = _events(
            parser,
            _v2_event(
                OpenCodeV2EventType.SESSION_NEXT_TOOL_SUCCESS,
                {
                    "sessionID": "ses_1",
                    "callID": "unknown_call",
                    "content": [{"type": "text", "text": "late"}],
                },
            ),
        )
        assert out == []

    def test_name_consumed_on_first_result(self) -> None:
        parser = OpenCodeV2EventParser()
        _tool_called(parser, call_id="call_1", tool="read")
        first = _events(
            parser,
            _v2_event(
                OpenCodeV2EventType.SESSION_NEXT_TOOL_SUCCESS,
                {
                    "sessionID": "ses_1",
                    "callID": "call_1",
                    "content": [{"type": "text", "text": "one"}],
                },
            ),
        )
        second = _events(
            parser,
            _v2_event(
                OpenCodeV2EventType.SESSION_NEXT_TOOL_SUCCESS,
                {
                    "sessionID": "ses_1",
                    "callID": "call_1",
                    "content": [{"type": "text", "text": "two"}],
                },
            ),
        )
        assert len(first) == 1
        assert second == []


class TestOpenCodeV2ToolInputDelta:
    def test_tool_input_delta_is_skipped(self) -> None:
        """Tool input is not streamed live to WebUI — ``session.next.tool.input.delta``
        must produce no event."""
        parser = OpenCodeV2EventParser()
        out = _events(
            parser,
            _v2_event(
                OpenCodeV2EventType.SESSION_NEXT_TOOL_INPUT_DELTA,
                {
                    "sessionID": "ses_1",
                    "assistantMessageID": "m1",
                    "callID": "call_1",
                    "delta": "partial input",
                },
            ),
        )
        assert out == []


# ---------------------------------------------------------------------------
# Permission / question — reader handles directly, parser returns empty
# ---------------------------------------------------------------------------


class TestOpenCodeV2PermissionQuestion:
    def test_permission_v2_asked_returns_empty(self) -> None:
        parser = OpenCodeV2EventParser()
        out = _events(
            parser,
            _v2_event(
                OpenCodeV2EventType.PERMISSION_V2_ASKED,
                {
                    "sessionID": "ses_1",
                    "id": "per_1",
                    "action": "bash",
                    "resources": ["exec"],
                    "source": {"type": "tool", "messageID": "m1", "callID": "call_1"},
                },
            ),
        )
        assert out == []

    def test_question_v2_asked_returns_empty(self) -> None:
        parser = OpenCodeV2EventParser()
        out = _events(
            parser,
            _v2_event(
                OpenCodeV2EventType.QUESTION_V2_ASKED,
                {
                    "sessionID": "ses_1",
                    "id": "que_1",
                    "questions": [
                        {
                            "question": "Which file?",
                            "header": "File",
                            "options": [{"label": "a.py", "description": "file a"}],
                        },
                    ],
                },
            ),
        )
        assert out == []


# ---------------------------------------------------------------------------
# Session error
# ---------------------------------------------------------------------------


class TestOpenCodeV2SessionError:
    def test_session_error_with_dict_error(self) -> None:
        parser = OpenCodeV2EventParser()
        out = _events(
            parser,
            _v2_event(
                OpenCodeV2EventType.SESSION_ERROR,
                {"sessionID": "ses_1", "error": {"type": "unknown", "message": "LLM timed out"}},
            ),
        )
        assert out == [TurnErroredEvent(message="LLM timed out")]

    def test_session_error_with_string_error(self) -> None:
        parser = OpenCodeV2EventParser()
        out = _events(
            parser,
            _v2_event(
                OpenCodeV2EventType.SESSION_ERROR,
                {"sessionID": "ses_1", "error": "Network failure"},
            ),
        )
        assert out == [TurnErroredEvent(message="Network failure")]


# ---------------------------------------------------------------------------
# Ignored / bookkeeping events
# ---------------------------------------------------------------------------


class TestOpenCodeV2IgnoredEvents:
    @pytest.mark.parametrize(
        "evt_type",
        [
            OpenCodeV2EventType.SERVER_CONNECTED,
            "session.next.step.started",
            "session.next.text.started",
            "session.next.tool.progress",
            "session.next.prompted",
            "some.unknown.event.type",
        ],
    )
    def test_bookkeeping_and_unknown_events_yield_nothing(
        self, evt_type: str
    ) -> None:
        parser = OpenCodeV2EventParser()
        out = _events(parser, _v2_event(evt_type, {"sessionID": "s"}))
        assert out == []


# ---------------------------------------------------------------------------
# Heartbeat — SSE comment, not a typed event
# ---------------------------------------------------------------------------


class TestOpenCodeV2Heartbeat:
    def test_heartbeat_comment_yields_nothing(self) -> None:
        parser = OpenCodeV2EventParser()
        assert list(parser.parse_line(": heartbeat")) == []

    def test_arbitrary_sse_comment_yields_nothing(self) -> None:
        parser = OpenCodeV2EventParser()
        assert list(parser.parse_line(": any comment")) == []


# ---------------------------------------------------------------------------
# V1 event handling — parser handles both V2 (data) and V1 (properties) envelopes
# ---------------------------------------------------------------------------


class TestV1EventHandling:
    def test_v1_part_delta_text(self) -> None:
        parser = OpenCodeV2EventParser()
        # V1 envelope: payload in "properties"
        out = _events(
            parser,
            {
                "id": "evt_1",
                "type": "message.part.delta",
                "properties": {"sessionID": "ses_1", "partID": "p1", "delta": "hello"},
            },
        )
        assert out == [TurnTextEvent(text="hello", part_id="p1")]

    def test_v1_part_delta_reasoning(self) -> None:
        parser = OpenCodeV2EventParser()
        # First, send a part.updated to set the part type to "reasoning"
        _events(
            parser,
            {
                "type": "message.part.updated",
                "properties": {
                    "sessionID": "ses_1",
                    "partID": "p1",
                    "part": {"id": "p1", "type": "reasoning"},
                },
            },
        )
        out = _events(
            parser,
            {
                "type": "message.part.delta",
                "properties": {"sessionID": "ses_1", "partID": "p1", "delta": "thinking..."},
            },
        )
        assert out == [TurnReasoningEvent(text="thinking...", part_id="p1")]

    def test_v1_part_updated_tool_use_and_result(self) -> None:
        parser = OpenCodeV2EventParser()
        out = _events(
            parser,
            {
                "type": "message.part.updated",
                "properties": {
                    "sessionID": "ses_1",
                    "partID": "p1",
                    "part": {
                        "id": "p1",
                        "type": "tool",
                        "tool": "bash",
                        "callID": "c1",
                        "state": {"status": "completed", "output": "done"},
                    },
                },
            },
        )
        assert len(out) == 2
        assert isinstance(out[0], TurnToolCallEvent)
        assert out[0].tool_name == "bash"
        assert out[0].call_id == "c1"
        assert isinstance(out[1], TurnToolResultEvent)
        assert out[1].call_id == "c1"
        assert out[1].tool_name == "bash"
        assert "done" in out[1].output
        assert out[1].error is None

    def test_v1_part_updated_tool_error_fills_error(self) -> None:
        parser = OpenCodeV2EventParser()
        out = _events(
            parser,
            {
                "type": "message.part.updated",
                "properties": {
                    "sessionID": "ses_1",
                    "part": {
                        "id": "p1",
                        "type": "tool",
                        "tool": "bash",
                        "callID": "c1",
                        "state": {"status": "error", "error": "command failed"},
                    },
                },
            },
        )
        assert len(out) == 2
        result = out[1]
        assert isinstance(result, TurnToolResultEvent)
        assert result.output == "command failed"
        assert result.error == "command failed"

    def test_v1_session_created_no_event(self) -> None:
        parser = OpenCodeV2EventParser()
        out = _events(
            parser,
            {
                "type": "session.created",
                "properties": {
                    "sessionID": "ses_child",
                    "info": {"id": "ses_child", "parentID": "ses_main"},
                },
            },
        )
        assert out == []

    def test_v1_envelope_properties_works(self) -> None:
        """V1 envelope with ``properties`` (not ``data``) is handled."""
        parser = OpenCodeV2EventParser()
        out = _events(
            parser,
            {
                "id": "evt_1",
                "type": "message.part.delta",
                "properties": {"sessionID": "ses_1", "delta": "text via properties"},
            },
        )
        assert out == [TurnTextEvent(text="text via properties")]


# ---------------------------------------------------------------------------
# Malformed input
# ---------------------------------------------------------------------------


class TestOpenCodeV2MalformedInput:
    def test_malformed_json_returns_empty(self) -> None:
        parser = OpenCodeV2EventParser()
        assert list(parser.parse_line("not valid json {{{")) == []

    def test_non_dict_payload_returns_empty(self) -> None:
        parser = OpenCodeV2EventParser()
        assert list(parser.parse_line(json.dumps(["not", "a", "dict"]))) == []

    def test_missing_data_field_returns_empty(self) -> None:
        parser = OpenCodeV2EventParser()
        out = _events(
            parser, {"id": "evt_1", "type": OpenCodeV2EventType.SESSION_NEXT_TEXT_DELTA}
        )
        assert out == []

    def test_non_dict_data_returns_empty(self) -> None:
        parser = OpenCodeV2EventParser()
        out = _events(
            parser,
            {
                "id": "evt_1",
                "type": OpenCodeV2EventType.SESSION_NEXT_TEXT_DELTA,
                "data": "not a dict",
            },
        )
        assert out == []

