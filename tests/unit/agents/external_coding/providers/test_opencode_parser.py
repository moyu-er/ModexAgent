"""Unit tests for OpenCodeEventParser — matched to real OpenCode JSON output.

OpenCode emits JSONL lines via ``opencode run --format json --thinking``.
Each line has ``{"type": ..., "sessionID": ..., "part": {...}}`` shape.

Event types:
- ``step_start`` / ``step_finish`` — bookkeeping
- ``text`` — complete text block (``part.time.end`` set)
- ``reasoning`` — complete reasoning block (``part.time.end`` set, needs ``--thinking``)
- ``tool_use`` — tool call+result (``part.state.status`` is ``completed`` or ``error``)
- ``error`` — session error
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

import pytest

from modex_agent.agents.external_coding import Emission, ExternalCodingEvent
from modex_agent.agents.external_coding.providers.opencode_parser import OpenCodeEventParser


def _line(payload: Mapping[str, object]) -> str:
    return json.dumps(payload)


def _emissions(parser: OpenCodeEventParser, *payloads: Mapping[str, object]) -> list[Emission]:
    out: list[Emission] = []
    for p in payloads:
        out.extend(parser.parse_line(_line(p)))
    return out


class TestOpenCodeEventParserABC:
    def test_is_provider_event_parser(self) -> None:
        from modex_agent.agents.external_coding import ProviderEventParser

        assert isinstance(OpenCodeEventParser(), ProviderEventParser)


class TestOpenCodeEventParserSessionIdCapture:
    def test_captures_session_id_from_first_event(self) -> None:
        parser = OpenCodeEventParser()
        parser.parse_line(_line({"type": "step_start", "sessionID": "oc-sid-1"}))
        assert parser.captured_session_id == "oc-sid-1"

    def test_captures_session_id_from_text_event(self) -> None:
        parser = OpenCodeEventParser()
        parser.parse_line(_line({"type": "step_start"}))
        parser.parse_line(_line({
            "type": "text",
            "sessionID": "oc-sid-2",
            "part": {"type": "text", "text": "hi", "time": {"end": 123}},
        }))
        assert parser.captured_session_id == "oc-sid-2"

    def test_session_id_none_until_seen(self) -> None:
        parser = OpenCodeEventParser()
        assert parser.captured_session_id is None

    def test_first_non_empty_session_id_wins(self) -> None:
        parser = OpenCodeEventParser()
        parser.parse_line(_line({"type": "step_start", "sessionID": "oc-sid-1"}))
        parser.parse_line(_line({
            "type": "text",
            "sessionID": "oc-sid-2",
            "part": {"type": "text", "text": "x", "time": {"end": 1}},
        }))
        assert parser.captured_session_id == "oc-sid-1"


class TestOpenCodeEventParserStepStart:
    def test_yields_nothing(self) -> None:
        parser = OpenCodeEventParser()
        assert _emissions(parser, {"type": "step_start", "part": {"type": "step-start"}}) == []


class TestOpenCodeEventParserStepFinish:
    def test_yields_nothing(self) -> None:
        parser = OpenCodeEventParser()
        assert _emissions(parser, {"type": "step_finish", "part": {"type": "step-finish"}}) == []


class TestOpenCodeEventParserText:
    def test_yields_text_delta_from_part(self) -> None:
        parser = OpenCodeEventParser()
        payload = {
            "type": "text",
            "part": {"type": "text", "text": "hello world", "time": {"end": 123}},
        }
        emissions = _emissions(parser, payload)
        assert len(emissions) == 1
        assert emissions[0].event is ExternalCodingEvent.TEXT_DELTA
        assert emissions[0].text == "hello world"

    def test_empty_text_yields_nothing(self) -> None:
        parser = OpenCodeEventParser()
        payload = {
            "type": "text",
            "part": {"type": "text", "text": "", "time": {"end": 123}},
        }
        assert _emissions(parser, payload) == []


class TestOpenCodeEventParserReasoning:
    def test_yields_thinking_emission(self) -> None:
        parser = OpenCodeEventParser()
        payload = {
            "type": "reasoning",
            "part": {"type": "reasoning", "text": "I should check the file first.", "time": {"end": 123}},
        }
        emissions = _emissions(parser, payload)
        assert len(emissions) == 1
        assert emissions[0].event is ExternalCodingEvent.THINKING
        assert emissions[0].text == "I should check the file first."

    def test_empty_reasoning_yields_nothing(self) -> None:
        parser = OpenCodeEventParser()
        payload = {
            "type": "reasoning",
            "part": {"type": "reasoning", "text": "", "time": {"end": 123}},
        }
        assert _emissions(parser, payload) == []


class TestOpenCodeEventParserToolUse:
    def test_yields_tool_use_and_result_for_completed_tool(self) -> None:
        parser = OpenCodeEventParser()
        payload = {
            "type": "tool_use",
            "part": {
                "type": "tool",
                "tool": "read",
                "id": "part_abc",
                "callID": "call_abc",
                "state": {
                    "status": "completed",
                    "input": {"path": "/etc/hosts"},
                    "output": "127.0.0.1 localhost\n",
                    "title": "Read /etc/hosts",
                    "metadata": {},
                    "time": {"start": 1, "end": 2},
                },
            },
        }
        emissions = _emissions(parser, payload)
        assert len(emissions) == 2
        assert emissions[0].event is ExternalCodingEvent.TOOL_USE
        assert emissions[0].tool_name == "read"
        assert emissions[0].call_id == "call_abc"
        assert "path" in (emissions[0].tool_input or "")
        assert "/etc/hosts" in (emissions[0].tool_input or "")
        assert emissions[1].event is ExternalCodingEvent.TOOL_RESULT
        assert emissions[1].call_id == "call_abc"
        assert emissions[1].output == "127.0.0.1 localhost\n"

    def test_yields_tool_use_and_error_result_for_failed_tool(self) -> None:
        parser = OpenCodeEventParser()
        payload = {
            "type": "tool_use",
            "part": {
                "type": "tool",
                "tool": "write",
                "id": "part_def",
                "callID": "call_def",
                "state": {
                    "status": "error",
                    "input": {"path": "/root/file"},
                    "error": "Permission denied",
                    "metadata": {},
                    "time": {"start": 1, "end": 2},
                },
            },
        }
        emissions = _emissions(parser, payload)
        assert len(emissions) == 2
        assert emissions[0].event is ExternalCodingEvent.TOOL_USE
        assert emissions[0].tool_name == "write"
        assert emissions[1].event is ExternalCodingEvent.TOOL_RESULT
        assert emissions[1].call_id == "call_def"
        assert "Permission denied" in (emissions[1].output or "")

    def test_tool_use_without_tool_name_yields_nothing(self) -> None:
        parser = OpenCodeEventParser()
        payload = {
            "type": "tool_use",
            "part": {"type": "tool", "state": {"status": "completed", "input": {}, "output": ""}},
        }
        assert _emissions(parser, payload) == []

    def test_tool_use_with_string_input_in_state(self) -> None:
        parser = OpenCodeEventParser()
        payload = {
            "type": "tool_use",
            "part": {
                "type": "tool",
                "tool": "bash",
                "id": "part_str",
                "callID": "call_str",
                "state": {
                    "status": "completed",
                    "input": {"cmd": "echo hi"},
                    "output": "hi\n",
                    "title": "Run echo hi",
                    "metadata": {},
                    "time": {"start": 1, "end": 2},
                },
            },
        }
        emissions = _emissions(parser, payload)
        assert len(emissions) == 2
        assert "echo hi" in (emissions[0].tool_input or "")

    def test_tool_use_mints_call_id_when_ids_absent(self) -> None:
        parser = OpenCodeEventParser()
        payload = {
            "type": "tool_use",
            "part": {
                "type": "tool",
                "tool": "bash",
                "state": {
                    "status": "completed",
                    "input": {"cmd": "ls"},
                    "output": "file.txt\n",
                    "title": "",
                    "metadata": {},
                    "time": {"start": 1, "end": 2},
                },
            },
        }
        emissions = _emissions(parser, payload)
        assert len(emissions) == 2
        use, result = emissions
        assert use.call_id is not None
        assert len(use.call_id) > 0
        assert use.call_id == result.call_id

    def test_tool_use_without_state_yields_use_only(self) -> None:
        parser = OpenCodeEventParser()
        payload = {
            "type": "tool_use",
            "part": {"type": "tool", "tool": "read", "id": "p1"},
        }
        emissions = _emissions(parser, payload)
        assert len(emissions) == 1
        assert emissions[0].event is ExternalCodingEvent.TOOL_USE
        assert emissions[0].tool_input == "{}"


class TestOpenCodeEventParserToolUseCallIdCorrelation:
    def test_completed_tool_shares_call_id(self) -> None:
        parser = OpenCodeEventParser()
        payload = {
            "type": "tool_use",
            "part": {
                "type": "tool",
                "tool": "read",
                "id": "part_xyz",
                "callID": "call_xyz",
                "state": {
                    "status": "completed",
                    "input": {"path": "README.md"},
                    "output": "# Project\n",
                    "title": "",
                    "metadata": {},
                    "time": {"start": 1, "end": 2},
                },
            },
        }
        emissions = _emissions(parser, payload)
        assert len(emissions) == 2
        use, result = emissions
        assert use.call_id == result.call_id == "call_xyz"

    def test_minted_call_id_shared_across_use_and_result(self) -> None:
        parser = OpenCodeEventParser()
        payload = {
            "type": "tool_use",
            "part": {
                "type": "tool",
                "tool": "bash",
                "state": {
                    "status": "completed",
                    "input": {"cmd": "echo hi"},
                    "output": "hi\n",
                    "title": "",
                    "metadata": {},
                    "time": {"start": 1, "end": 2},
                },
            },
        }
        emissions = _emissions(parser, payload)
        assert len(emissions) == 2
        use, result = emissions
        assert use.call_id is not None
        assert len(use.call_id) > 0
        assert use.call_id == result.call_id


class TestOpenCodeEventParserError:
    def test_yields_error_from_message_key(self) -> None:
        parser = OpenCodeEventParser()
        payload = {"type": "error", "message": "something failed"}
        [emission] = _emissions(parser, payload)
        assert emission.event is ExternalCodingEvent.ERROR
        assert emission.message == "something failed"

    def test_error_with_error_key_fallback(self) -> None:
        parser = OpenCodeEventParser()
        payload = {"type": "error", "error": "alt message"}
        [emission] = _emissions(parser, payload)
        assert emission.message == "alt message"

    def test_error_with_nested_error_dict(self) -> None:
        parser = OpenCodeEventParser()
        payload = {
            "type": "error",
            "error": {
                "name": "UnknownError",
                "data": {"message": "Unexpected server error.", "ref": "err_123"},
            },
        }
        [emission] = _emissions(parser, payload)
        assert emission.message == "Unexpected server error."

    def test_error_with_nested_error_dict_name_fallback(self) -> None:
        parser = OpenCodeEventParser()
        payload = {"type": "error", "error": {"name": "TimeoutError"}}
        [emission] = _emissions(parser, payload)
        assert emission.message == "TimeoutError"


class TestOpenCodeEventParserUnknownTypes:
    @pytest.mark.parametrize(
        "payload",
        [
            {"type": "status", "msg": "thinking"},
            {"type": "usage", "tokens": 100},
            {"type": "metrics", "duration_ms": 50},
            {"type": "log", "level": "info", "msg": "..."},
        ],
    )
    def test_unknown_types_yield_nothing(self, payload: dict[str, object]) -> None:
        parser = OpenCodeEventParser()
        assert _emissions(parser, payload) == []


class TestOpenCodeEventParserInvalidLine:
    def test_garbage_line_yields_nothing(self) -> None:
        parser = OpenCodeEventParser()
        assert list(parser.parse_line("not-json{")) == []
        assert list(parser.parse_line("")) == []

    def test_empty_object_yields_nothing(self) -> None:
        parser = OpenCodeEventParser()
        assert list(parser.parse_line(_line({}))) == []


class TestOpenCodeEventParserRealisticStream:
    def test_full_stream_with_text_reasoning_tool_and_session_id(self) -> None:
        parser = OpenCodeEventParser()
        events: list[dict[str, object]] = [
            {"type": "step_start", "sessionID": "oc-sid-real", "part": {"type": "step-start"}},
            {
                "type": "reasoning",
                "sessionID": "oc-sid-real",
                "part": {"type": "reasoning", "text": "Need to read the file.", "time": {"end": 1}},
            },
            {
                "type": "text",
                "sessionID": "oc-sid-real",
                "part": {"type": "text", "text": "Let me read the file.", "time": {"end": 2}},
            },
            {
                "type": "tool_use",
                "sessionID": "oc-sid-real",
                "part": {
                    "type": "tool",
                    "tool": "read",
                    "id": "part_real",
                    "callID": "call_real",
                    "state": {
                        "status": "completed",
                        "input": {"path": "/etc/hosts"},
                        "output": "127.0.0.1 localhost\n",
                        "title": "",
                        "metadata": {},
                        "time": {"start": 1, "end": 2},
                    },
                },
            },
            {
                "type": "text",
                "sessionID": "oc-sid-real",
                "part": {"type": "text", "text": "The file contains localhost.", "time": {"end": 3}},
            },
            {"type": "step_finish", "sessionID": "oc-sid-real", "part": {"type": "step-finish"}},
        ]
        out = _emissions(parser, *events)
        assert len(out) == 5

        assert out[0].event is ExternalCodingEvent.THINKING
        assert out[0].text == "Need to read the file."

        assert out[1].event is ExternalCodingEvent.TEXT_DELTA
        assert out[1].text == "Let me read the file."

        assert out[2].event is ExternalCodingEvent.TOOL_USE
        assert out[2].tool_name == "read"
        assert out[2].call_id == "call_real"
        assert "/etc/hosts" in (out[2].tool_input or "")

        assert out[3].event is ExternalCodingEvent.TOOL_RESULT
        assert out[3].call_id == "call_real"
        assert out[3].output == "127.0.0.1 localhost\n"

        assert out[4].event is ExternalCodingEvent.TEXT_DELTA
        assert out[4].text == "The file contains localhost."

        assert parser.captured_session_id == "oc-sid-real"


class TestOpenCodeRealFixture:
    """Parser tests against real opencode stdout captured from a live session.

    Fixture: ``opencode_stdout_fixture.jsonl`` — captured from
    ``opencode run --format json --dangerously-skip-permissions --thinking``
    with a prompt that triggers: reasoning → tool_use → text (two steps).
    """

    _FIXTURE_PATH = (
        Path(__file__).parent / "opencode_stdout_fixture.jsonl"
    )

    def _parse_fixture(self) -> list[Emission]:
        parser = OpenCodeEventParser()
        emissions: list[Emission] = []
        with self._FIXTURE_PATH.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                emissions.extend(parser.parse_line(line))
        return emissions

    def test_captures_session_id_from_fixture(self) -> None:
        parser = OpenCodeEventParser()
        with self._FIXTURE_PATH.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    parser.parse_line(line)
        assert parser.captured_session_id is not None
        assert parser.captured_session_id.startswith("ses_")

    def test_fixture_produces_expected_event_sequence(self) -> None:
        emissions = self._parse_fixture()
        events = [e.event for e in emissions]
        assert events == [
            ExternalCodingEvent.THINKING,
            ExternalCodingEvent.TOOL_USE,
            ExternalCodingEvent.TOOL_RESULT,
            ExternalCodingEvent.TEXT_DELTA,
        ]

    def test_fixture_reasoning_text_not_empty(self) -> None:
        emissions = self._parse_fixture()
        reasoning = [e for e in emissions if e.event is ExternalCodingEvent.THINKING]
        assert len(reasoning) == 1
        assert "README" in reasoning[0].text

    def test_fixture_text_delta_not_empty(self) -> None:
        emissions = self._parse_fixture()
        texts = [e for e in emissions if e.event is ExternalCodingEvent.TEXT_DELTA]
        assert len(texts) == 1
        assert len(texts[0].text) > 10

    def test_fixture_tool_use_has_name_and_call_id(self) -> None:
        emissions = self._parse_fixture()
        tool_uses = [e for e in emissions if e.event is ExternalCodingEvent.TOOL_USE]
        assert len(tool_uses) == 1
        assert tool_uses[0].tool_name == "read"
        assert tool_uses[0].call_id is not None

    def test_fixture_tool_result_has_output(self) -> None:
        emissions = self._parse_fixture()
        results = [e for e in emissions if e.event is ExternalCodingEvent.TOOL_RESULT]
        assert len(results) == 1
        assert "README" in (results[0].output or "")
