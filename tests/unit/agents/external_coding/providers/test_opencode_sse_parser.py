from __future__ import annotations

import json
from collections.abc import Mapping

import pytest

from modex_agent.agents.external_coding import Emission, ExternalCodingEvent
from modex_agent.agents.external_coding.providers.opencode_sse_parser import (
    OpenCodeSSEParser,
    SSEEventType,
)


def _sse_line(payload: Mapping[str, object]) -> str:
    return json.dumps(payload)


def _emissions(parser: OpenCodeSSEParser, *payloads: Mapping[str, object]) -> list[Emission]:
    out: list[Emission] = []
    for p in payloads:
        out.extend(parser.parse_line(_sse_line(p)))
    return out


class TestOpenCodeSSEParserSessionId:
    def test_captures_session_id_from_first_event(self) -> None:
        parser = OpenCodeSSEParser()
        parser.parse_line(_sse_line({
            "id": "evt_1",
            "type": "message.part.delta",
            "properties": {"sessionID": "ses_abc", "partID": "p1", "field": "text", "delta": "hi"},
        }))
        assert parser.captured_session_id == "ses_abc"

    def test_session_id_none_until_seen(self) -> None:
        parser = OpenCodeSSEParser()
        assert parser.captured_session_id is None


class TestOpenCodeSSEParserTextDelta:
    def test_text_delta_yields_text_delta_emission(self) -> None:
        parser = OpenCodeSSEParser()
        out = _emissions(parser, {
            "id": "evt_1",
            "type": SSEEventType.MESSAGE_PART_DELTA,
            "properties": {"sessionID": "ses_1", "messageID": "m1", "partID": "p1", "field": "text", "delta": "Hello"},
        })
        assert len(out) == 1
        assert out[0].event is ExternalCodingEvent.TEXT_DELTA
        assert out[0].text == "Hello"

    def test_text_delta_multiple_fragments_preserve_order(self) -> None:
        parser = OpenCodeSSEParser()
        out = _emissions(
            parser,
            {"id": "e1", "type": SSEEventType.MESSAGE_PART_DELTA, "properties": {"sessionID": "s", "partID": "p", "field": "text", "delta": "Hello "}},
            {"id": "e2", "type": SSEEventType.MESSAGE_PART_DELTA, "properties": {"sessionID": "s", "partID": "p", "field": "text", "delta": "world!"}},
        )
        assert len(out) == 2
        assert out[0].text == "Hello "
        assert out[1].text == "world!"

    def test_reasoning_delta_yields_thinking_emission(self) -> None:
        parser = OpenCodeSSEParser()
        out = _emissions(
            parser,
            {"id": "e0", "type": SSEEventType.MESSAGE_PART_UPDATED, "properties": {"sessionID": "ses_1", "part": {"id": "p1", "type": "reasoning", "text": ""}}},
            {"id": "evt_1", "type": SSEEventType.MESSAGE_PART_DELTA, "properties": {"sessionID": "ses_1", "partID": "p1", "field": "text", "delta": "Thinking..."}},
        )
        assert len(out) == 1
        assert out[0].event is ExternalCodingEvent.THINKING
        assert out[0].text == "Thinking..."

    def test_text_delta_without_prior_part_updated_defaults_to_text(self) -> None:
        parser = OpenCodeSSEParser()
        out = _emissions(parser, {
            "id": "evt_1",
            "type": SSEEventType.MESSAGE_PART_DELTA,
            "properties": {"sessionID": "ses_1", "partID": "p_unknown", "field": "text", "delta": "hello"},
        })
        assert len(out) == 1
        assert out[0].event is ExternalCodingEvent.TEXT_DELTA

    def test_tool_part_delta_is_dropped_not_misclassified_as_text(self) -> None:
        """When a tool part receives ``message.part.delta`` events (opencode
        streams tool output incrementally), the parser must NOT classify them
        as TEXT_DELTA. Tool output is delivered via ``message.part.updated``
        with ``status: completed``; deltas for tool parts are dropped."""
        parser = OpenCodeSSEParser()
        out = _emissions(
            parser,
            {"id": "e0", "type": SSEEventType.MESSAGE_PART_UPDATED, "properties": {"sessionID": "ses_1", "part": {"id": "p_tool", "type": "tool", "tool": "read", "callID": "c1", "state": {"status": "running"}}}},
            {"id": "e1", "type": SSEEventType.MESSAGE_PART_DELTA, "properties": {"sessionID": "ses_1", "partID": "p_tool", "field": "text", "delta": "tool output fragment"}},
            {"id": "e2", "type": SSEEventType.MESSAGE_PART_DELTA, "properties": {"sessionID": "ses_1", "partID": "p_tool", "field": "text", "delta": " more output"}},
        )
        assert len(out) == 1
        assert out[0].event is ExternalCodingEvent.TOOL_USE
        assert not any(e.event is ExternalCodingEvent.TEXT_DELTA for e in out)


from typing import Any  # noqa: E402


class TestOpenCodeSSEParserToolUse:
    def _tool_updated_event(self, status: str = "completed", output: str | None = None, error: str | None = None) -> dict[str, Any]:
        state: dict[str, Any] = {"status": status, "input": {"path": "/tmp/x"}}
        if output is not None:
            state["output"] = output
        if error is not None:
            state["error"] = {"message": error}
        return {
            "id": "evt_t1",
            "type": SSEEventType.MESSAGE_PART_UPDATED,
            "properties": {
                "sessionID": "ses_1",
                "part": {"type": "tool", "tool": "read", "callID": "call_1", "state": state},
            },
        }

    def test_tool_completed_yields_tool_use_and_tool_result(self) -> None:
        parser = OpenCodeSSEParser()
        out = _emissions(parser, self._tool_updated_event(status="completed", output="file contents"))
        assert len(out) == 2
        assert out[0].event is ExternalCodingEvent.TOOL_USE
        assert out[0].tool_name == "read"
        assert out[0].call_id == "call_1"
        assert out[1].event is ExternalCodingEvent.TOOL_RESULT
        assert out[1].call_id == "call_1"
        assert out[1].output == "file contents"

    def test_tool_error_yields_tool_use_and_tool_result_with_error(self) -> None:
        parser = OpenCodeSSEParser()
        out = _emissions(parser, self._tool_updated_event(status="error", error="File not found"))
        assert len(out) == 2
        assert out[0].event is ExternalCodingEvent.TOOL_USE
        assert out[1].event is ExternalCodingEvent.TOOL_RESULT
        assert "File not found" in (out[1].output or "")

    def test_tool_running_yields_tool_use_only(self) -> None:
        parser = OpenCodeSSEParser()
        out = _emissions(parser, self._tool_updated_event(status="running"))
        assert len(out) == 1
        assert out[0].event is ExternalCodingEvent.TOOL_USE
        assert out[0].tool_name == "read"

    def test_tool_pending_then_completed_does_not_duplicate_tool_use(self) -> None:
        """When a tool part transitions pending→completed, the parser must NOT
        emit a second TOOL_USE. opencode fires ``message.part.updated`` twice
        for the same tool part (once at start, once at completion); without
        deduplication the frontend sees two ``tool_call_start`` events and
        renders duplicate tool cards."""
        parser = OpenCodeSSEParser()
        out = _emissions(
            parser,
            self._tool_updated_event(status="running"),
            self._tool_updated_event(status="completed", output="file contents"),
        )
        tool_use_count = sum(1 for e in out if e.event is ExternalCodingEvent.TOOL_USE)
        tool_result_count = sum(1 for e in out if e.event is ExternalCodingEvent.TOOL_RESULT)
        assert tool_use_count == 1, f"Expected 1 TOOL_USE, got {tool_use_count}"
        assert tool_result_count == 1

    def test_tool_pending_skipped_then_running_emits_tool_use(self) -> None:
        """A ``pending`` event (input not yet ready) must NOT emit TOOL_USE.
        The subsequent ``running`` event carries the complete input and emits
        the single TOOL_USE. This prevents the frontend from rendering a tool
        card with empty/partial args that never gets updated."""
        parser = OpenCodeSSEParser()
        out = _emissions(
            parser,
            self._tool_updated_event(status="pending"),
            self._tool_updated_event(status="running"),
            self._tool_updated_event(status="completed", output="done"),
        )
        tool_use_count = sum(1 for e in out if e.event is ExternalCodingEvent.TOOL_USE)
        tool_result_count = sum(1 for e in out if e.event is ExternalCodingEvent.TOOL_RESULT)
        assert tool_use_count == 1, f"Expected 1 TOOL_USE, got {tool_use_count}"
        assert tool_result_count == 1
        tool_use = next(e for e in out if e.event is ExternalCodingEvent.TOOL_USE)
        assert tool_use.tool_input  # not empty — has the complete input

    def test_text_part_updated_with_time_end_yields_nothing(self) -> None:
        parser = OpenCodeSSEParser()
        out = _emissions(parser, {
            "id": "evt_t",
            "type": SSEEventType.MESSAGE_PART_UPDATED,
            "properties": {
                "sessionID": "ses_1",
                "part": {"type": "text", "text": "complete text", "time": {"end": 123}},
            },
        })
        assert len(out) == 0


class TestOpenCodeSSEParserSessionError:
    def test_session_error_yields_error_emission(self) -> None:
        parser = OpenCodeSSEParser()
        out = _emissions(parser, {
            "id": "evt_e1",
            "type": SSEEventType.SESSION_ERROR,
            "properties": {"sessionID": "ses_1", "error": {"message": "LLM timed out"}},
        })
        assert len(out) == 1
        assert out[0].event is ExternalCodingEvent.ERROR
        assert out[0].message == "LLM timed out"

    def test_session_error_string_form(self) -> None:
        parser = OpenCodeSSEParser()
        out = _emissions(parser, {
            "id": "evt_e2",
            "type": SSEEventType.SESSION_ERROR,
            "properties": {"sessionID": "ses_1", "error": "Network error"},
        })
        assert len(out) == 1
        assert out[0].event is ExternalCodingEvent.ERROR
        assert out[0].message == "Network error"


class TestOpenCodeSSEParserIgnoredEvents:
    @pytest.mark.parametrize("evt_type", [
        SSEEventType.SESSION_STATUS,
        SSEEventType.MESSAGE_UPDATED,
        SSEEventType.SERVER_CONNECTED,
        SSEEventType.SERVER_HEARTBEAT,
        SSEEventType.PERMISSION_ASKED,
    ])
    def test_bookkeeping_events_yield_nothing(self, evt_type: str) -> None:
        parser = OpenCodeSSEParser()
        out = _emissions(parser, {"id": "e", "type": evt_type, "properties": {"sessionID": "s"}})
        assert len(out) == 0


class TestOpenCodeSSEParserChildSession:
    def test_child_session_id_tracked(self) -> None:
        parser = OpenCodeSSEParser()
        parser.set_main_session("ses_main")
        _emissions(
            parser,
            {"id": "e1", "type": SSEEventType.MESSAGE_PART_DELTA, "properties": {"sessionID": "ses_main", "field": "text", "delta": "main"}},
            {"id": "e2", "type": SSEEventType.MESSAGE_PART_DELTA, "properties": {"sessionID": "ses_child", "field": "text", "delta": "child"}},
        )
        assert "ses_child" in parser.child_session_ids

    def test_child_session_delta_still_emitted(self) -> None:
        """Child session deltas are emitted with source_session_id set —
        they belong to subagent sessions spawned by the ``task`` tool and
        are tagged with the child session ID for routing by the agent harness."""
        parser = OpenCodeSSEParser()
        parser.set_main_session("ses_main")
        out = _emissions(parser, {
            "id": "e1",
            "type": SSEEventType.MESSAGE_PART_DELTA,
            "properties": {"sessionID": "ses_child", "field": "text", "delta": "child text"},
        })
        assert len(out) == 1
        assert out[0].source_session_id == "ses_child"
        assert out[0].text == "child text"
