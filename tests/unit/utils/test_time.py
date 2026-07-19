"""Tests for the unified timestamp utilities (ADR-0029 §2)."""

from __future__ import annotations

import time

from modex_agent.utils.time import now_ms, now_s


def test_now_ms_returns_int() -> None:
    ts = now_ms()
    assert isinstance(ts, int)


def test_now_ms_is_unix_epoch_milliseconds() -> None:
    before = int(time.time() * 1000)
    ts = now_ms()
    after = int(time.time() * 1000) + 1
    assert before <= ts <= after


def test_now_s_returns_float() -> None:
    ts = now_s()
    assert isinstance(ts, float)


def test_now_s_is_unix_epoch_seconds() -> None:
    before = time.time()
    ts = now_s()
    after = time.time() + 0.001
    assert before <= ts <= after


def test_now_ms_and_now_s_are_consistent() -> None:
    """now_ms() should be within 1 second of now_s() * 1000."""
    s = now_s()
    ms = now_ms()
    assert abs(ms - int(s * 1000)) < 1500


def test_now_ms_monotonic_non_decreasing_within_call() -> None:
    """Two successive calls should not go backwards (within timer resolution)."""
    a = now_ms()
    b = now_ms()
    assert b >= a
