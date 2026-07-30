"""Child-session event handling for ``OpenCodeSSEParser``.

When ``set_main_session`` is configured, events whose ``sessionID`` differs
from the main session are no longer dropped. The parser tracks the child
session id in ``child_session_ids`` and tags every ``Emission`` produced
from such events with ``source_session_id=<child sid>`` so downstream
consumers (``ExternalCodingAgent._handle_emission``) can route them.

Main-session and sessionless events keep ``source_session_id=None``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from modex_agent.agents.external_coding import Emission, ExternalCodingEvent
from modex_agent.agents.external_coding.providers.opencode_sse_parser import (
    OpenCodeSSEParser,
    SSEEventType,
)

MAIN_SID = "ses_main"
CHILD_SID = "ses_child"


def _sse_line(payload: Mapping[str, object]) -> str:
    return json.dumps(payload)


def _emissions(parser: OpenCodeSSEParser, *payloads: Mapping[str, object]) -> list[Emission]:
    out: list[Emission] = []
    for p in payloads:
        out.extend(parser.parse_line(_sse_line(p)))
    return out


def _delta_event(sid: str, delta: str, part_id: str = "p1") -> dict[str, Any]:
    return {
        "id": "evt_1",
        "type": SSEEventType.MESSAGE_PART_DELTA,
        "properties": {"sessionID": sid, "partID": part_id, "field": "text", "delta": delta},
    }


def _tool_updated_event(
    sid: str,
    status: str = "completed",
    output: str | None = None,
    call_id: str = "call_1",
) -> dict[str, Any]:
    state: dict[str, Any] = {"status": status, "input": {"path": "foo.py"}}
    if output is not None:
        state["output"] = output
    return {
        "id": "evt_t1",
        "type": SSEEventType.MESSAGE_PART_UPDATED,
        "properties": {
            "sessionID": sid,
            "part": {"type": "tool", "tool": "read", "callID": call_id, "state": state},
        },
    }


class TestChildSessionSourceSessionId:
    def test_main_session_delta_has_no_source_session_id(self) -> None:
        parser = OpenCodeSSEParser()
        parser.set_main_session(MAIN_SID)
        out = _emissions(parser, _delta_event(MAIN_SID, "hello"))
        assert len(out) == 1
        assert out[0].event is ExternalCodingEvent.TEXT_DELTA
        assert out[0].text == "hello"
        assert out[0].source_session_id is None

    def test_child_session_delta_has_source_session_id(self) -> None:
        parser = OpenCodeSSEParser()
        parser.set_main_session(MAIN_SID)
        out = _emissions(parser, _delta_event(CHILD_SID, "world"))
        assert len(out) == 1
        assert out[0].event is ExternalCodingEvent.TEXT_DELTA
        assert out[0].text == "world"
        assert out[0].source_session_id == CHILD_SID

    def test_child_session_id_tracked_in_property(self) -> None:
        parser = OpenCodeSSEParser()
        parser.set_main_session(MAIN_SID)
        _emissions(parser, _delta_event(CHILD_SID, "child text"))
        assert CHILD_SID in parser.child_session_ids

    def test_child_session_tool_part_updated_has_source_session_id(self) -> None:
        parser = OpenCodeSSEParser()
        parser.set_main_session(MAIN_SID)
        out = _emissions(
            parser,
            _tool_updated_event(CHILD_SID, status="completed", output="file contents"),
        )
        assert len(out) == 2
        tool_use = next(e for e in out if e.event is ExternalCodingEvent.TOOL_USE)
        tool_result = next(e for e in out if e.event is ExternalCodingEvent.TOOL_RESULT)
        assert tool_use.source_session_id == CHILD_SID
        assert tool_result.source_session_id == CHILD_SID

    def test_same_child_sid_twice_has_one_child_session_id_entry(self) -> None:
        parser = OpenCodeSSEParser()
        parser.set_main_session(MAIN_SID)
        out = _emissions(
            parser,
            _delta_event(CHILD_SID, "first", part_id="p1"),
            _delta_event(CHILD_SID, "second", part_id="p2"),
        )
        assert len(out) == 2
        assert all(e.source_session_id == CHILD_SID for e in out)
        assert parser.child_session_ids == frozenset({CHILD_SID})

    def test_malformed_json_returns_empty_iterator(self) -> None:
        parser = OpenCodeSSEParser()
        parser.set_main_session(MAIN_SID)
        out = list(parser.parse_line("not valid json {{{"))
        assert out == []

    def test_empty_session_id_does_not_trigger_child_logic(self) -> None:
        parser = OpenCodeSSEParser()
        parser.set_main_session(MAIN_SID)
        out = _emissions(
            parser,
            {
                "id": "evt_1",
                "type": SSEEventType.MESSAGE_PART_DELTA,
                "properties": {"sessionID": "", "partID": "p1", "field": "text", "delta": "no sid"},
            },
        )
        assert len(out) == 1
        assert out[0].source_session_id is None
        assert parser.child_session_ids == frozenset()
