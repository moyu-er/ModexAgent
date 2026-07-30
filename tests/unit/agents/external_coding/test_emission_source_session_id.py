"""Unit tests for the ``Emission.source_session_id`` field.

Coverage:
  - defaults to ``None`` when omitted (backward compat with main-session
    emissions sourced from ``opencode run --format json`` stdout).
  - accepts a string value (child session ID from the SSE event stream).
  - appears in ``model_dump()`` output.
  - survives a ``model_validate()`` round-trip.
  - rejects non-string types (e.g. ``int``) with ``ValidationError``.
  - existing ``Emission`` constructions without the field still work.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from modex_agent.agents.external_coding.events import ExternalCodingEvent
from modex_agent.agents.external_coding.types import Emission


class TestEmissionSourceSessionId:
    """``source_session_id`` field behavior and backward compatibility."""

    def test_defaults_to_none_when_omitted(self) -> None:
        e = Emission(event=ExternalCodingEvent.TEXT_DELTA, text="hello")
        assert e.source_session_id is None

    def test_can_be_set_to_string(self) -> None:
        e = Emission(
            event=ExternalCodingEvent.TEXT_DELTA,
            text="child text",
            source_session_id="session_child_abc",
        )
        assert e.source_session_id == "session_child_abc"

    def test_model_dump_includes_source_session_id(self) -> None:
        e = Emission(
            event=ExternalCodingEvent.TOOL_USE,
            tool_name="bash",
            source_session_id="session_child_abc",
        )
        dumped = e.model_dump()
        assert "source_session_id" in dumped
        assert dumped["source_session_id"] == "session_child_abc"

    def test_model_validate_round_trip_preserves_value(self) -> None:
        original = Emission(
            event=ExternalCodingEvent.TOOL_RESULT,
            call_id="c1",
            output="done",
            source_session_id="session_child_xyz",
        )
        restored = Emission.model_validate(original.model_dump())
        assert restored == original
        assert restored.source_session_id == "session_child_xyz"

    def test_int_value_raises_validation_error(self) -> None:
        with pytest.raises(ValidationError):
            Emission(
                event=ExternalCodingEvent.ERROR,
                message="boom",
                source_session_id=12345,  # type: ignore[arg-type]
            )

    def test_existing_construction_without_field_still_works(self) -> None:
        e = Emission(
            event=ExternalCodingEvent.THINKING,
            text="reasoning",
        )
        assert e.event is ExternalCodingEvent.THINKING
        assert e.text == "reasoning"
        assert e.source_session_id is None
