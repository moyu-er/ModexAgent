"""Tests for the Snowflake-based tool-call id minting (core.ids)."""

from __future__ import annotations

from modex_agent.core.ids import next_call_id


def test_next_call_id_has_call_prefix() -> None:
    call_id = next_call_id()
    assert call_id.startswith("call_")
    # Snowflake body is a decimal integer (time-ordered, compact).
    assert call_id.removeprefix("call_").isdigit()


def test_next_call_ids_are_unique_and_monotonic() -> None:
    ids = [next_call_id() for _ in range(100)]
    assert len(set(ids)) == 100
    snowflakes = [int(i.removeprefix("call_")) for i in ids]
    assert snowflakes == sorted(snowflakes)
