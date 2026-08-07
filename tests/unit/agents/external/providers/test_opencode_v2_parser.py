"""Unit tests for ``OpenCodeV2EventParser`` — V2 SSE event parsing.

The V2 protocol surface (``/api/event``) uses the envelope
``{id, type, data, metadata?, durable?, location?}`` with the payload in
``data`` (NOT ``properties`` like V1). Heartbeats are SSE comment lines
(``: heartbeat``), not typed events.

These tests cover every event-type mapping in the spec, child-session
detection, heartbeat stripping, V1-event rejection, and malformed input.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import pytest

from modex_agent.agents.external import Emission, ExternalEvent
from modex_agent.agents.external.providers.opencode.v2_parser import (
    OpenCodeV2EventParser,
    OpenCodeV2EventType,
)

MAIN_SID = "ses_main"
CHILD_SID = "ses_child"


def _sse_line(payload: Mapping[str, object]) -> str:
    return json.dumps(payload)


def _emissions(parser: OpenCodeV2EventParser, *payloads: Mapping[str, object]) -> list[Emission]:
    out: list[Emission] = []
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


# ---------------------------------------------------------------------------
# Text / reasoning deltas
# ---------------------------------------------------------------------------


class TestOpenCodeV2TextDelta:
    def test_text_delta_yields_text_delta_emission(self) -> None:
        parser = OpenCodeV2EventParser()
        out = _emissions(
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
        assert len(out) == 1
        assert out[0].event is ExternalEvent.TEXT_DELTA
        assert out[0].text == "Hello"

    def test_text_delta_multiple_fragments_preserve_order(self) -> None:
        parser = OpenCodeV2EventParser()
        out = _emissions(
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
        assert len(out) == 2
        assert out[0].text == "Hello "
        assert out[1].text == "world!"

    def test_text_delta_empty_string_yields_nothing(self) -> None:
        parser = OpenCodeV2EventParser()
        out = _emissions(
            parser,
            _v2_event(
                OpenCodeV2EventType.SESSION_NEXT_TEXT_DELTA,
                {"sessionID": "s", "delta": ""},
            ),
        )
        assert out == []

    def test_text_delta_missing_delta_yields_nothing(self) -> None:
        parser = OpenCodeV2EventParser()
        out = _emissions(
            parser,
            _v2_event(
                OpenCodeV2EventType.SESSION_NEXT_TEXT_DELTA,
                {"sessionID": "s"},
            ),
        )
        assert out == []


class TestOpenCodeV2ReasoningDelta:
    def test_reasoning_delta_yields_thinking_emission(self) -> None:
        parser = OpenCodeV2EventParser()
        out = _emissions(
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
        assert len(out) == 1
        assert out[0].event is ExternalEvent.THINKING
        assert out[0].text == "Thinking..."


# ---------------------------------------------------------------------------
# Tool events
# ---------------------------------------------------------------------------


class TestOpenCodeV2ToolCalled:
    def test_tool_called_yields_tool_use_emission(self) -> None:
        parser = OpenCodeV2EventParser()
        out = _emissions(
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
        assert len(out) == 1
        assert out[0].event is ExternalEvent.TOOL_USE
        assert out[0].tool_name == "read"
        assert out[0].call_id == "call_1"
        assert json.loads(out[0].tool_input or "{}") == {"path": "/tmp/foo.py"}

    def test_tool_called_with_string_input(self) -> None:
        parser = OpenCodeV2EventParser()
        out = _emissions(
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
        assert out[0].tool_input == "raw string input"

    def test_tool_called_missing_tool_name_yields_nothing(self) -> None:
        parser = OpenCodeV2EventParser()
        out = _emissions(
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


class TestOpenCodeV2ToolSuccess:
    def test_tool_success_with_text_content(self) -> None:
        parser = OpenCodeV2EventParser()
        out = _emissions(
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
        assert out[0].event is ExternalEvent.TOOL_RESULT
        assert out[0].call_id == "call_1"
        assert out[0].output == "file contents here"

    def test_tool_success_with_multiple_text_content_joined(self) -> None:
        parser = OpenCodeV2EventParser()
        out = _emissions(
            parser,
            _v2_event(
                OpenCodeV2EventType.SESSION_NEXT_TOOL_SUCCESS,
                {
                    "sessionID": "ses_1",
                    "assistantMessageID": "m1",
                    "callID": "call_1",
                    "structured": {},
                    "content": [
                        {"type": "text", "text": "line1\n"},
                        {"type": "text", "text": "line2\n"},
                    ],
                    "provider": {"executed": True},
                },
            ),
        )
        assert len(out) == 1
        assert out[0].output == "line1\nline2\n"

    def test_tool_success_with_structured_when_no_content(self) -> None:
        parser = OpenCodeV2EventParser()
        out = _emissions(
            parser,
            _v2_event(
                OpenCodeV2EventType.SESSION_NEXT_TOOL_SUCCESS,
                {
                    "sessionID": "ses_1",
                    "assistantMessageID": "m1",
                    "callID": "call_1",
                    "structured": {"rows": 42, "matched": 3},
                    "content": [],
                    "provider": {"executed": True},
                },
            ),
        )
        assert len(out) == 1
        assert out[0].event is ExternalEvent.TOOL_RESULT
        assert json.loads(out[0].output or "{}") == {"rows": 42, "matched": 3}

    def test_tool_success_empty_content_and_structured_yields_nothing(self) -> None:
        parser = OpenCodeV2EventParser()
        out = _emissions(
            parser,
            _v2_event(
                OpenCodeV2EventType.SESSION_NEXT_TOOL_SUCCESS,
                {
                    "sessionID": "ses_1",
                    "assistantMessageID": "m1",
                    "callID": "call_1",
                    "structured": {},
                    "content": [],
                    "provider": {"executed": True},
                },
            ),
        )
        assert out == []


class TestOpenCodeV2ToolFailed:
    def test_tool_failed_yields_tool_result_with_error_message(self) -> None:
        parser = OpenCodeV2EventParser()
        out = _emissions(
            parser,
            _v2_event(
                OpenCodeV2EventType.SESSION_NEXT_TOOL_FAILED,
                {
                    "sessionID": "ses_1",
                    "assistantMessageID": "m1",
                    "callID": "call_1",
                    "error": {"type": "unknown", "message": "File not found"},
                    "provider": {"executed": True},
                },
            ),
        )
        assert len(out) == 1
        assert out[0].event is ExternalEvent.TOOL_RESULT
        assert out[0].call_id == "call_1"
        assert out[0].output == "File not found"

    def test_tool_failed_with_string_error(self) -> None:
        parser = OpenCodeV2EventParser()
        out = _emissions(
            parser,
            _v2_event(
                OpenCodeV2EventType.SESSION_NEXT_TOOL_FAILED,
                {
                    "sessionID": "ses_1",
                    "assistantMessageID": "m1",
                    "callID": "call_1",
                    "error": "Network error",
                    "provider": {"executed": True},
                },
            ),
        )
        assert len(out) == 1
        assert out[0].output == "Network error"


class TestOpenCodeV2ToolInputDelta:
    def test_tool_input_delta_is_skipped(self) -> None:
        """Tool input is not streamed live to WebUI — ``session.next.tool.input.delta``
        must produce no emission."""
        parser = OpenCodeV2EventParser()
        out = _emissions(
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
        out = _emissions(
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
        out = _emissions(
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
        out = _emissions(
            parser,
            _v2_event(
                OpenCodeV2EventType.SESSION_ERROR,
                {"sessionID": "ses_1", "error": {"type": "unknown", "message": "LLM timed out"}},
            ),
        )
        assert len(out) == 1
        assert out[0].event is ExternalEvent.ERROR
        assert out[0].message == "LLM timed out"

    def test_session_error_with_string_error(self) -> None:
        parser = OpenCodeV2EventParser()
        out = _emissions(
            parser,
            _v2_event(
                OpenCodeV2EventType.SESSION_ERROR,
                {"sessionID": "ses_1", "error": "Network failure"},
            ),
        )
        assert len(out) == 1
        assert out[0].message == "Network failure"


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
    def test_bookkeeping_and_unknown_events_yield_nothing(self, evt_type: str) -> None:
        parser = OpenCodeV2EventParser()
        out = _emissions(parser, _v2_event(evt_type, {"sessionID": "s"}))
        assert out == []


# ---------------------------------------------------------------------------
# Heartbeat — SSE comment, not a typed event
# ---------------------------------------------------------------------------


class TestOpenCodeV2Heartbeat:
    def test_heartbeat_comment_yields_nothing(self) -> None:
        parser = OpenCodeV2EventParser()
        out = list(parser.parse_line(": heartbeat"))
        assert out == []

    def test_arbitrary_sse_comment_yields_nothing(self) -> None:
        parser = OpenCodeV2EventParser()
        out = list(parser.parse_line(": any comment"))
        assert out == []


# ---------------------------------------------------------------------------
# V1 event handling — parser handles both V2 (data) and V1 (properties) envelopes
# ---------------------------------------------------------------------------


class TestV1EventHandling:
    def test_v1_part_delta_text(self) -> None:
        parser = OpenCodeV2EventParser()
        parser.add_main_session("ses_1")
        # V1 envelope: payload in "properties"
        out = _emissions(
            parser,
            {
                "id": "evt_1",
                "type": "message.part.delta",
                "properties": {"sessionID": "ses_1", "partID": "p1", "delta": "hello"},
            },
        )
        assert len(out) == 1
        assert out[0].event is ExternalEvent.TEXT_DELTA
        assert out[0].text == "hello"

    def test_v1_part_delta_reasoning(self) -> None:
        parser = OpenCodeV2EventParser()
        parser.add_main_session("ses_1")
        # First, send a part.updated to set the part type to "reasoning"
        _emissions(
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
        out = _emissions(
            parser,
            {
                "type": "message.part.delta",
                "properties": {"sessionID": "ses_1", "partID": "p1", "delta": "thinking..."},
            },
        )
        assert len(out) == 1
        assert out[0].event is ExternalEvent.THINKING

    def test_v1_part_updated_tool_use_and_result(self) -> None:
        parser = OpenCodeV2EventParser()
        parser.add_main_session("ses_1")
        out = _emissions(
            parser,
            {
                "type": "message.part.updated",
                "properties": {
                    "sessionID": "ses_1",
                    "partID": "p1",
                    "part": {
                        "type": "tool",
                        "tool": "bash",
                        "callID": "c1",
                        "state": {"status": "completed", "output": "done"},
                    },
                },
            },
        )
        assert len(out) == 2
        assert out[0].event is ExternalEvent.TOOL_USE
        assert out[0].tool_name == "bash"
        assert out[1].event is ExternalEvent.TOOL_RESULT
        assert out[1].call_id == "c1"
        assert "done" in out[1].output

    def test_v1_session_created_no_emission(self) -> None:
        parser = OpenCodeV2EventParser()
        out = _emissions(
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
        """V1 envelope with ``properties`` (not ``data``) is now handled."""
        parser = OpenCodeV2EventParser()
        out = _emissions(
            parser,
            {
                "id": "evt_1",
                "type": "message.part.delta",
                "properties": {"sessionID": "ses_1", "delta": "text via properties"},
            },
        )
        assert len(out) == 1
        assert out[0].text == "text via properties"


# ---------------------------------------------------------------------------
# Malformed input
# ---------------------------------------------------------------------------


class TestOpenCodeV2MalformedInput:
    def test_malformed_json_returns_empty(self) -> None:
        parser = OpenCodeV2EventParser()
        out = list(parser.parse_line("not valid json {{{"))
        assert out == []

    def test_non_dict_payload_returns_empty(self) -> None:
        parser = OpenCodeV2EventParser()
        out = list(parser.parse_line(json.dumps(["not", "a", "dict"])))
        assert out == []

    def test_missing_data_field_returns_empty(self) -> None:
        parser = OpenCodeV2EventParser()
        out = _emissions(
            parser, {"id": "evt_1", "type": OpenCodeV2EventType.SESSION_NEXT_TEXT_DELTA}
        )
        assert out == []

    def test_non_dict_data_returns_empty(self) -> None:
        parser = OpenCodeV2EventParser()
        out = _emissions(
            parser,
            {
                "id": "evt_1",
                "type": OpenCodeV2EventType.SESSION_NEXT_TEXT_DELTA,
                "data": "not a dict",
            },
        )
        assert out == []


# ---------------------------------------------------------------------------
# Child session detection
# ---------------------------------------------------------------------------


class TestOpenCodeV2ChildSession:
    def test_main_session_delta_has_no_source_session_id(self) -> None:
        parser = OpenCodeV2EventParser()
        parser.add_main_session(MAIN_SID)
        out = _emissions(
            parser,
            _v2_event(
                OpenCodeV2EventType.SESSION_NEXT_TEXT_DELTA,
                {"sessionID": MAIN_SID, "delta": "hello"},
            ),
        )
        assert len(out) == 1
        assert out[0].source_session_id is None

    def test_child_session_delta_has_source_session_id(self) -> None:
        parser = OpenCodeV2EventParser()
        parser.add_main_session(MAIN_SID)
        out = _emissions(
            parser,
            _v2_event(
                OpenCodeV2EventType.SESSION_NEXT_TEXT_DELTA,
                {"sessionID": CHILD_SID, "delta": "child text"},
            ),
        )
        assert len(out) == 1
        assert out[0].source_session_id == CHILD_SID
        assert out[0].text == "child text"

    def test_child_session_id_tracked_in_property(self) -> None:
        parser = OpenCodeV2EventParser()
        parser.add_main_session(MAIN_SID)
        _emissions(
            parser,
            _v2_event(
                OpenCodeV2EventType.SESSION_NEXT_TEXT_DELTA,
                {"sessionID": CHILD_SID, "delta": "child"},
            ),
        )
        assert CHILD_SID in parser.child_session_ids

    def test_child_session_tool_called_has_source_session_id(self) -> None:
        parser = OpenCodeV2EventParser()
        parser.add_main_session(MAIN_SID)
        out = _emissions(
            parser,
            _v2_event(
                OpenCodeV2EventType.SESSION_NEXT_TOOL_CALLED,
                {
                    "sessionID": CHILD_SID,
                    "assistantMessageID": "m1",
                    "callID": "call_1",
                    "tool": "read",
                    "input": {"path": "foo.py"},
                    "provider": {"executed": False},
                },
            ),
        )
        assert len(out) == 1
        assert out[0].source_session_id == CHILD_SID

    def test_same_child_sid_twice_has_one_entry(self) -> None:
        parser = OpenCodeV2EventParser()
        parser.add_main_session(MAIN_SID)
        out = _emissions(
            parser,
            _v2_event(
                OpenCodeV2EventType.SESSION_NEXT_TEXT_DELTA,
                {"sessionID": CHILD_SID, "delta": "first"},
                event_id="e1",
            ),
            _v2_event(
                OpenCodeV2EventType.SESSION_NEXT_TEXT_DELTA,
                {"sessionID": CHILD_SID, "delta": "second"},
                event_id="e2",
            ),
        )
        assert len(out) == 2
        assert all(e.source_session_id == CHILD_SID for e in out)
        assert parser.child_session_ids == frozenset({CHILD_SID})

    def test_no_main_session_set_never_marks_child(self) -> None:
        """Without ``add_main_session``, no session is treated as a child."""
        parser = OpenCodeV2EventParser()
        out = _emissions(
            parser,
            _v2_event(
                OpenCodeV2EventType.SESSION_NEXT_TEXT_DELTA,
                {"sessionID": "ses_any", "delta": "text"},
            ),
        )
        assert len(out) == 1
        assert out[0].source_session_id is None
        assert parser.child_session_ids == frozenset()

    def test_empty_session_id_does_not_trigger_child_logic(self) -> None:
        parser = OpenCodeV2EventParser()
        parser.add_main_session(MAIN_SID)
        out = _emissions(
            parser,
            _v2_event(
                OpenCodeV2EventType.SESSION_NEXT_TEXT_DELTA,
                {"sessionID": "", "delta": "no sid"},
            ),
        )
        assert len(out) == 1
        assert out[0].source_session_id is None
        assert parser.child_session_ids == frozenset()


# ---------------------------------------------------------------------------
# Multi main-session — add_main_session / remove_main_session
# ---------------------------------------------------------------------------


MAIN_SID_2 = "ses_main_2"
MAIN_SID_3 = "ses_main_3"


class TestMultiMainSession:
    def test_two_main_sessions_both_not_child(self) -> None:
        parser = OpenCodeV2EventParser()
        parser.add_main_session(MAIN_SID)
        parser.add_main_session(MAIN_SID_2)
        out = _emissions(
            parser,
            _v2_event(
                OpenCodeV2EventType.SESSION_NEXT_TEXT_DELTA,
                {"sessionID": MAIN_SID, "delta": "a"},
                event_id="e1",
            ),
            _v2_event(
                OpenCodeV2EventType.SESSION_NEXT_TEXT_DELTA,
                {"sessionID": MAIN_SID_2, "delta": "b"},
                event_id="e2",
            ),
        )
        assert len(out) == 2
        assert all(e.source_session_id is None for e in out)
        assert parser.child_session_ids == frozenset()

    def test_remove_main_session_marks_remaining_as_still_main(self) -> None:
        parser = OpenCodeV2EventParser()
        parser.add_main_session(MAIN_SID)
        parser.add_main_session(MAIN_SID_2)
        parser.remove_main_session(MAIN_SID)

        out = _emissions(
            parser,
            _v2_event(
                OpenCodeV2EventType.SESSION_NEXT_TEXT_DELTA,
                {"sessionID": MAIN_SID_2, "delta": "still main"},
            ),
        )
        assert len(out) == 1
        assert out[0].source_session_id is None

    def test_remove_main_session_marks_removed_as_child(self) -> None:
        parser = OpenCodeV2EventParser()
        parser.add_main_session(MAIN_SID)
        parser.add_main_session(MAIN_SID_2)
        parser.remove_main_session(MAIN_SID)

        out = _emissions(
            parser,
            _v2_event(
                OpenCodeV2EventType.SESSION_NEXT_TEXT_DELTA,
                {"sessionID": MAIN_SID, "delta": "now child"},
            ),
        )
        assert len(out) == 1
        assert out[0].source_session_id == MAIN_SID
        assert MAIN_SID in parser.child_session_ids

    def test_unregistered_session_treated_as_child(self) -> None:
        parser = OpenCodeV2EventParser()
        parser.add_main_session(MAIN_SID)
        out = _emissions(
            parser,
            _v2_event(
                OpenCodeV2EventType.SESSION_NEXT_TEXT_DELTA,
                {"sessionID": "ses_never_registered", "delta": "child"},
            ),
        )
        assert len(out) == 1
        assert out[0].source_session_id == "ses_never_registered"
        assert "ses_never_registered" in parser.child_session_ids

    def test_add_main_session_is_idempotent(self) -> None:
        parser = OpenCodeV2EventParser()
        parser.add_main_session(MAIN_SID)
        parser.add_main_session(MAIN_SID)
        parser.add_main_session(MAIN_SID)
        out = _emissions(
            parser,
            _v2_event(
                OpenCodeV2EventType.SESSION_NEXT_TEXT_DELTA,
                {"sessionID": MAIN_SID, "delta": "main"},
            ),
        )
        assert len(out) == 1
        assert out[0].source_session_id is None

    def test_remove_main_session_on_never_added_is_noop(self) -> None:
        parser = OpenCodeV2EventParser()
        parser.add_main_session(MAIN_SID)
        parser.remove_main_session(MAIN_SID_3)
        out = _emissions(
            parser,
            _v2_event(
                OpenCodeV2EventType.SESSION_NEXT_TEXT_DELTA,
                {"sessionID": MAIN_SID, "delta": "main"},
            ),
        )
        assert len(out) == 1
        assert out[0].source_session_id is None

    def test_remove_all_main_sessions_disables_child_detection(self) -> None:
        parser = OpenCodeV2EventParser()
        parser.add_main_session(MAIN_SID)
        parser.remove_main_session(MAIN_SID)
        out = _emissions(
            parser,
            _v2_event(
                OpenCodeV2EventType.SESSION_NEXT_TEXT_DELTA,
                {"sessionID": "ses_any", "delta": "text"},
            ),
        )
        assert len(out) == 1
        assert out[0].source_session_id is None
        assert parser.child_session_ids == frozenset()

    def test_three_main_sessions_all_not_child(self) -> None:
        parser = OpenCodeV2EventParser()
        parser.add_main_session(MAIN_SID)
        parser.add_main_session(MAIN_SID_2)
        parser.add_main_session(MAIN_SID_3)
        out = _emissions(
            parser,
            _v2_event(
                OpenCodeV2EventType.SESSION_NEXT_TEXT_DELTA,
                {"sessionID": MAIN_SID_3, "delta": "third main"},
            ),
        )
        assert len(out) == 1
        assert out[0].source_session_id is None
        assert parser.child_session_ids == frozenset()
