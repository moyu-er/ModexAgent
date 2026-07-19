"""Unified timestamp utilities (ADR-0029 §2).

Single source of truth for Unix-epoch timestamps across the framework.
`now_ms()` returns integer milliseconds; `now_s()` returns float seconds.
Both are UTC Unix epoch — no timezone conversion. Storage and runtime
state columns use millisecond integers exclusively; float seconds are
provided for compatibility with stdlib time APIs and performance timers.

`core.session_id` re-exports `now_ms` for backward compatibility with
existing callers; new code should import directly from this module.
"""

from __future__ import annotations

import time

__all__ = ["now_ms", "now_s"]


def now_ms() -> int:
    """Current Unix time in milliseconds (UTC)."""
    return int(time.time() * 1000)


def now_s() -> float:
    """Current Unix time in seconds (UTC, float)."""
    return time.time()
