"""Unit tests for PiEventParser (T4)."""

from __future__ import annotations

import json
from collections.abc import Mapping

import pytest

from modex_agent.agents.external_coding import Emission, ExternalCodingEvent
from modex_agent.agents.external_coding.providers.pi_parser import PiEventParser


def _line(payload: Mapping[str, object]) -> str:
    return json.dumps(payload)


def _emissions(parser: PiEventParser, *payloads: Mapping[str, object]) -> list[Emission]:
    out: list[Emission] = []
    for p in payloads:
        out.extend(parser.parse_line(_line(p)))
    return out


class TestPiEventParserABC:
    def test_is_provider_event_parser(self) -> None:
        from modex_agent.agents.external_coding import ProviderEventParser

        assert isinstance(PiEventParser(), ProviderEventParser)


class TestPiEventParserSessionIdCapture:
    def test_captures_session_id_from_first_event(self) -> None:
        parser = PiEventParser()
        parser.parse_line(_line({"type": "agent_start", "session_id": "psid-1"}))
        assert parser.captured_session_id == "psid-1"

    def test_captures_session_id_from_subsequent_event(self) -> None:
        parser = PiEventParser()
        parser.parse_line(_line({"type": "agent_start"}))
        parser.parse_line(_line({"type": "turn_start", "session_id": "psid-late"}))
        assert parser.captured_session_id == "psid-late"

    def test_captured_session_id_is_none_until_seen(self) -> None:
        parser = PiEventParser()
        assert parser.captured_session_id is None
        parser.parse_line(_line({"type": "agent_start"}))
        assert parser.captured_session_id is None

    def test_first_non_empty_session_id_wins(self) -> None:
        parser = PiEventParser()
        parser.parse_line(_line({"type": "agent_start", "session_id": "psid-1"}))
        parser.parse_line(_line({"type": "turn_start", "session_id": "psid-2"}))
        assert parser.captured_session_id == "psid-1"

    def test_session_id_capture_from_tool_event(self) -> None:
        parser = PiEventParser()
        parser.parse_line(
            _line(
                {
                    "type": "tool_execution_start",
                    "session_id": "psid-tool",
                    "tool_call_id": "tc-1",
                    "tool_name": "bash",
                    "args": {},
                }
            )
        )
        assert parser.captured_session_id == "psid-tool"


class TestPiEventParserMessageUpdateTextDelta:
    def test_yields_text_delta(self) -> None:
        parser = PiEventParser()
        emissions = _emissions(
            parser,
            {"type": "message_update", "update": {"subtype": "text_delta", "delta": "hello"}},
        )
        assert len(emissions) == 1
        assert emissions[0].event is ExternalCodingEvent.TEXT_DELTA
        assert emissions[0].text == "hello"

    def test_text_delta_with_markup_stripped(self) -> None:
        parser = PiEventParser()
        payload = {
            "type": "message_update",
            "update": {
                "subtype": "text_delta",
                "delta": "before call:ReadFile{path=\"/tmp\"}<tool_call|>after",
            },
        }
        emissions = _emissions(parser, payload)
        assert len(emissions) == 1
        assert emissions[0].event is ExternalCodingEvent.TEXT_DELTA
        assert emissions[0].text == "before after"

    def test_text_delta_with_tool_response_stripped(self) -> None:
        parser = PiEventParser()
        payload = {
            "type": "message_update",
            "update": {
                "subtype": "text_delta",
                "delta": "before <|tool_response|>result<|end_tool_response|>after",
            },
        }
        emissions = _emissions(parser, payload)
        assert len(emissions) == 1
        assert emissions[0].text == "before after"

    def test_text_delta_with_control_token_stripped(self) -> None:
        parser = PiEventParser()
        payload = {
            "type": "message_update",
            "update": {
                "subtype": "text_delta",
                "delta": "a<|control_token|>b",
            },
        }
        emissions = _emissions(parser, payload)
        assert emissions[0].text == "ab"

    def test_text_delta_only_markup_yields_no_emission(self) -> None:
        parser = PiEventParser()
        payload = {
            "type": "message_update",
            "update": {
                "subtype": "text_delta",
                "delta": "call:Foo{args}<tool_call|>",
            },
        }
        emissions = _emissions(parser, payload)
        assert emissions == []


class TestPiEventParserMessageUpdateThinkingDelta:
    def test_yields_thinking(self) -> None:
        parser = PiEventParser()
        emissions = _emissions(
            parser,
            {"type": "message_update", "update": {"subtype": "thinking_delta", "delta": "hmm..."}},
        )
        assert len(emissions) == 1
        assert emissions[0].event is ExternalCodingEvent.THINKING
        assert emissions[0].text == "hmm..."

    def test_thinking_delta_markup_stripped(self) -> None:
        parser = PiEventParser()
        payload = {
            "type": "message_update",
            "update": {
                "subtype": "thinking_delta",
                "delta": "thinking call:Foo{}<tool_call|> continues",
            },
        }
        emissions = _emissions(parser, payload)
        assert len(emissions) == 1
        assert emissions[0].event is ExternalCodingEvent.THINKING
        assert emissions[0].text == "thinking  continues"


class TestPiEventParserMessageUpdateDualDelta:
    def test_message_update_with_both_text_and_thinking_yields_two(self) -> None:
        parser = PiEventParser()
        # Rare case: one message_update carries both deltas.
        payload = {
            "type": "message_update",
            "update": {"text_delta": "answer", "thinking_delta": "thought"},
        }
        emissions = _emissions(parser, payload)
        assert len(emissions) == 2
        events = [e.event for e in emissions]
        assert ExternalCodingEvent.TEXT_DELTA in events
        assert ExternalCodingEvent.THINKING in events
        thinking_em = next(e for e in emissions if e.event is ExternalCodingEvent.THINKING)
        text_em = next(e for e in emissions if e.event is ExternalCodingEvent.TEXT_DELTA)
        assert thinking_em.text == "thought"
        assert text_em.text == "answer"


class TestPiEventParserToolExecutionStart:
    def test_yields_tool_use(self) -> None:
        parser = PiEventParser()
        payload = {
            "type": "tool_execution_start",
            "tool_call_id": "tc-1",
            "tool_name": "bash",
            "args": {"cmd": "ls"},
        }
        emissions = _emissions(parser, payload)
        assert len(emissions) == 1
        assert emissions[0].event is ExternalCodingEvent.TOOL_USE
        assert emissions[0].tool_name == "bash"
        assert "ls" in (emissions[0].tool_input or "")

    def test_tool_use_preserves_call_id_on_text_field(self) -> None:
        parser = PiEventParser()
        payload = {
            "type": "tool_execution_start",
            "tool_call_id": "tc-7",
            "tool_name": "read_file",
            "args": {"path": "/etc/hosts"},
        }
        [emission] = _emissions(parser, payload)
        # Emission.call_id exists but the canonical place is tool_use's
        # tool_input. Day one we don't carry call_id on TOOL_USE; verify
        # only the documented fields.
        assert emission.tool_name == "read_file"
        assert emission.tool_input is not None


class TestPiEventParserToolExecutionEnd:
    def test_yields_tool_result(self) -> None:
        parser = PiEventParser()
        payload = {
            "type": "tool_execution_end",
            "tool_call_id": "tc-1",
            "result": "exit 0\nfile contents",
        }
        emissions = _emissions(parser, payload)
        assert len(emissions) == 1
        assert emissions[0].event is ExternalCodingEvent.TOOL_RESULT
        assert emissions[0].call_id == "tc-1"
        assert emissions[0].output == "exit 0\nfile contents"

    def test_tool_result_with_dict_result_serialised_as_string(self) -> None:
        parser = PiEventParser()
        payload = {
            "type": "tool_execution_end",
            "tool_call_id": "tc-2",
            "result": {"stdout": "ok", "stderr": ""},
        }
        [emission] = _emissions(parser, payload)
        assert emission.event is ExternalCodingEvent.TOOL_RESULT
        assert emission.call_id == "tc-2"
        assert "ok" in (emission.output or "")


class TestPiEventParserError:
    def test_yields_error(self) -> None:
        parser = PiEventParser()
        payload = {"type": "error", "message": "something broke"}
        emissions = _emissions(parser, payload)
        assert len(emissions) == 1
        assert emissions[0].event is ExternalCodingEvent.ERROR
        assert emissions[0].message == "something broke"

    def test_error_uses_error_key_as_fallback(self) -> None:
        parser = PiEventParser()
        payload = {"type": "error", "error": "fallback message"}
        emissions = _emissions(parser, payload)
        assert emissions[0].message == "fallback message"


class TestPiEventParserNoOpEvents:
    @pytest.mark.parametrize(
        "payload",
        [
            {"type": "agent_start"},
            {"type": "turn_start"},
            {"type": "turn_end"},
            {"type": "auto_retry_end"},
            {"type": "status", "message": "thinking"},
            {"type": "usage", "tokens": 100},
        ],
    )
    def test_unknown_or_status_events_yield_nothing(self, payload: dict[str, object]) -> None:
        parser = PiEventParser()
        assert _emissions(parser, payload) == []


class TestPiEventParserInvalidLine:
    def test_garbage_line_yields_nothing(self) -> None:
        parser = PiEventParser()
        assert list(parser.parse_line("not-json{")) == []
        assert list(parser.parse_line("")) == []

    def test_empty_object_yields_nothing(self) -> None:
        parser = PiEventParser()
        assert list(parser.parse_line(_line({}))) == []


class TestPiEventParserDeltaSplitting:
    """Markup tokens may be split across multiple `message_update` lines.

    The parser holds a per-instance buffer so split markup tokens are
    reassembled correctly. Plain text adjacent to a partial markup
    prefix is emitted as soon as it is provably clean.
    """

    def test_split_call_opening(self) -> None:
        parser = PiEventParser()
        emissions: list[Emission] = []
        emissions.extend(
            parser.parse_line(
                _line(
                    {
                        "type": "message_update",
                        "update": {"subtype": "text_delta", "delta": "before cal"},
                    }
                )
            )
        )
        assert len(emissions) == 1
        assert emissions[0].event is ExternalCodingEvent.TEXT_DELTA
        assert emissions[0].text == "before "
        emissions.extend(
            parser.parse_line(
                _line(
                    {
                        "type": "message_update",
                        "update": {"subtype": "text_delta", "delta": "l:Foo{}<tool_"},
                    }
                )
            )
        )
        assert len(emissions) == 1
        emissions.extend(
            parser.parse_line(
                _line(
                    {
                        "type": "message_update",
                        "update": {"subtype": "text_delta", "delta": "call|> after"},
                    }
                )
            )
        )
        assert len(emissions) == 2
        assert emissions[1].event is ExternalCodingEvent.TEXT_DELTA
        assert emissions[1].text == " after"

    def test_split_tool_response_closing(self) -> None:
        parser = PiEventParser()
        emissions: list[Emission] = []
        emissions.extend(
            parser.parse_line(
                _line(
                    {
                        "type": "message_update",
                        "update": {
                            "subtype": "text_delta",
                            "delta": "before <|tool_respons",
                        },
                    }
                )
            )
        )
        assert len(emissions) == 1
        assert emissions[0].text == "before "
        emissions.extend(
            parser.parse_line(
                _line(
                    {
                        "type": "message_update",
                        "update": {
                            "subtype": "text_delta",
                            "delta": "e|>ok<|end_to",
                        },
                    }
                )
            )
        )
        assert len(emissions) == 1
        emissions.extend(
            parser.parse_line(
                _line(
                    {
                        "type": "message_update",
                        "update": {
                            "subtype": "text_delta",
                            "delta": "ol_response|>after",
                        },
                    }
                )
            )
        )
        assert len(emissions) == 2
        assert emissions[1].text == "after"

    def test_split_control_token(self) -> None:
        parser = PiEventParser()
        emissions: list[Emission] = []
        emissions.extend(
            parser.parse_line(
                _line(
                    {
                        "type": "message_update",
                        "update": {"subtype": "text_delta", "delta": "a<|control_to"},
                    }
                )
            )
        )
        assert len(emissions) == 1
        assert emissions[0].text == "a"
        emissions.extend(
            parser.parse_line(
                _line(
                    {
                        "type": "message_update",
                        "update": {"subtype": "text_delta", "delta": "ken|>b"},
                    }
                )
            )
        )
        assert len(emissions) == 2
        assert emissions[1].text == "b"

    def test_buffer_isolated_per_parser_instance(self) -> None:
        a = PiEventParser()
        b = PiEventParser()
        a.parse_line(
            _line(
                {
                    "type": "message_update",
                    "update": {"subtype": "text_delta", "delta": "hello cal"},
                }
            )
        )
        emissions = list(
            b.parse_line(
                _line(
                    {
                        "type": "message_update",
                        "update": {"subtype": "text_delta", "delta": "world"},
                    }
                )
            )
        )
        assert len(emissions) == 1
        assert emissions[0].text == "world"

    def test_emission_before_tool_call_then_after(self) -> None:
        parser = PiEventParser()
        payload = {
            "type": "message_update",
            "update": {
                "subtype": "text_delta",
                "delta": "first call:Foo{}<tool_call|>second <|control_token|>third",
            },
        }
        [emission] = _emissions(parser, payload)
        assert emission.text == "first second third"


class TestPiEventParserSequentialStream:
    def test_realistic_stream_of_events(self) -> None:
        parser = PiEventParser()
        events: list[dict[str, object]] = [
            {"type": "agent_start", "session_id": "psid-42"},
            {"type": "turn_start"},
            {
                "type": "message_update",
                "update": {"subtype": "text_delta", "delta": "Looking at "},
            },
            {
                "type": "message_update",
                "update": {
                    "subtype": "text_delta",
                    "delta": "the code call:ReadFile{path=\"/tmp\"}<tool_call|>",
                },
            },
            {
                "type": "tool_execution_start",
                "tool_call_id": "tc-1",
                "tool_name": "ReadFile",
                "args": {"path": "/tmp"},
            },
            {
                "type": "tool_execution_end",
                "tool_call_id": "tc-1",
                "result": "file contents",
            },
            {
                "type": "message_update",
                "update": {
                    "subtype": "text_delta",
                    "delta": "I see <|control_token|>the data",
                },
            },
            {"type": "turn_end"},
            {"type": "error", "message": "ok actually done"},
        ]
        out = _emissions(parser, *events)
        # 6 emissions: 2 text deltas (split because delta 2's tool-call
        # markup is stripped before emission), 1 tool_use, 1 tool_result,
        # 1 text delta (control token dropped), 1 error.
        assert len(out) == 6
        assert out[0].event is ExternalCodingEvent.TEXT_DELTA
        assert out[0].text == "Looking at "
        assert out[1].event is ExternalCodingEvent.TEXT_DELTA
        assert out[1].text == "the code "
        assert out[2].event is ExternalCodingEvent.TOOL_USE
        assert out[2].tool_name == "ReadFile"
        assert out[3].event is ExternalCodingEvent.TOOL_RESULT
        assert out[3].call_id == "tc-1"
        assert out[3].output == "file contents"
        assert out[4].event is ExternalCodingEvent.TEXT_DELTA
        assert out[4].text == "I see the data"
        assert out[5].event is ExternalCodingEvent.ERROR
        assert parser.captured_session_id == "psid-42"


class TestPiEventParserToolCallIdCorrelation:
    """Pi's tool_execution_start and tool_execution_end share tool_call_id.

    The parser reads ``tool_call_id`` on BOTH events so the TOOL_USE and
    TOOL_RESULT emissions carry the same non-empty call_id — the contract
    the WebUI projection pairs persisted tool blocks by.
    """

    def test_tool_use_carries_call_id_matching_result(self) -> None:
        parser = PiEventParser()
        use_emissions = _emissions(
            parser,
            {
                "type": "tool_execution_start",
                "tool_call_id": "tc-shared",
                "tool_name": "bash",
                "args": {"cmd": "ls"},
            },
        )
        result_emissions = _emissions(
            parser,
            {
                "type": "tool_execution_end",
                "tool_call_id": "tc-shared",
                "result": "file.txt",
            },
        )
        assert len(use_emissions) == 1
        assert len(result_emissions) == 1
        use, result = use_emissions[0], result_emissions[0]
        assert use.event is ExternalCodingEvent.TOOL_USE
        assert result.event is ExternalCodingEvent.TOOL_RESULT
        assert use.call_id == "tc-shared"
        assert result.call_id == "tc-shared"
        assert use.call_id == result.call_id

    def test_realistic_stream_shares_call_id_across_start_and_end(self) -> None:
        parser = PiEventParser()
        out = _emissions(
            parser,
            {"type": "tool_execution_start", "tool_call_id": "tc-9",
             "tool_name": "ReadFile", "args": {"path": "/tmp"}},
            {"type": "tool_execution_end", "tool_call_id": "tc-9",
             "result": "contents"},
        )
        use, result = out
        assert use.event is ExternalCodingEvent.TOOL_USE
        assert result.event is ExternalCodingEvent.TOOL_RESULT
        assert use.call_id == result.call_id == "tc-9"
