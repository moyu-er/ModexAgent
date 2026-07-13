"""Unit tests for the `ExternalCodingEvent` StrEnum."""

from __future__ import annotations

import pytest

from modex_agent.agents.external_coding import ExternalCodingEvent


class TestExternalCodingEvent:
    """The 5 day-one kinds; value == name.lower() for StrEnum."""

    def test_has_five_day_one_kinds(self) -> None:
        kinds = {
            ExternalCodingEvent.TEXT_DELTA,
            ExternalCodingEvent.THINKING,
            ExternalCodingEvent.TOOL_USE,
            ExternalCodingEvent.TOOL_RESULT,
            ExternalCodingEvent.ERROR,
        }
        assert kinds == set(ExternalCodingEvent)

    def test_values_match_documented_wire_format(self) -> None:
        assert ExternalCodingEvent.TEXT_DELTA.value == "text_delta"
        assert ExternalCodingEvent.THINKING.value == "thinking"
        assert ExternalCodingEvent.TOOL_USE.value == "tool_use"
        assert ExternalCodingEvent.TOOL_RESULT.value == "tool_result"
        assert ExternalCodingEvent.ERROR.value == "error"

    @pytest.mark.parametrize("name", ["text_delta", "thinking", "tool_use", "tool_result", "error"])
    def test_value_lookup_round_trips(self, name: str) -> None:
        # StrEnum lets us look up by value, which is how parser callbacks
        # will type-switch on the producer side.
        assert ExternalCodingEvent(name) is getattr(ExternalCodingEvent, name.upper())

    def test_unknown_value_raises_value_error(self) -> None:
        with pytest.raises(ValueError):
            ExternalCodingEvent("not_a_real_event")

    def test_is_subclass_of_str(self) -> None:
        # StrEnum instances compare equal to their string value — this is
        # how parsers feed the raw event-type field straight into the enum.
        assert ExternalCodingEvent.TEXT_DELTA == "text_delta"
        assert ExternalCodingEvent.TEXT_DELTA == "text_delta"
