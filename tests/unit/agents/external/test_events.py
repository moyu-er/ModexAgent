"""Unit tests for the `ExternalEvent` StrEnum."""

from __future__ import annotations

import pytest

from modex_agent.agents.external import ExternalEvent


class TestExternalEvent:
    """The 5 day-one kinds; value == name.lower() for StrEnum."""

    def test_has_five_day_one_kinds(self) -> None:
        kinds = {
            ExternalEvent.TEXT_DELTA,
            ExternalEvent.THINKING,
            ExternalEvent.TOOL_USE,
            ExternalEvent.TOOL_RESULT,
            ExternalEvent.ERROR,
        }
        assert kinds == set(ExternalEvent)

    def test_values_match_documented_wire_format(self) -> None:
        assert ExternalEvent.TEXT_DELTA.value == "text_delta"
        assert ExternalEvent.THINKING.value == "thinking"
        assert ExternalEvent.TOOL_USE.value == "tool_use"
        assert ExternalEvent.TOOL_RESULT.value == "tool_result"
        assert ExternalEvent.ERROR.value == "error"

    @pytest.mark.parametrize("name", ["text_delta", "thinking", "tool_use", "tool_result", "error"])
    def test_value_lookup_round_trips(self, name: str) -> None:
        # StrEnum lets us look up by value, which is how parser callbacks
        # will type-switch on the producer side.
        assert ExternalEvent(name) is getattr(ExternalEvent, name.upper())

    def test_unknown_value_raises_value_error(self) -> None:
        with pytest.raises(ValueError):
            ExternalEvent("not_a_real_event")

    def test_is_subclass_of_str(self) -> None:
        # StrEnum instances compare equal to their string value — this is
        # how parsers feed the raw event-type field straight into the enum.
        assert ExternalEvent.TEXT_DELTA == "text_delta"
        assert ExternalEvent.TEXT_DELTA == "text_delta"
