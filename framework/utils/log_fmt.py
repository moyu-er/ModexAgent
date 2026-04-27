"""Structured logging helpers for observability.

Provides a lightweight ``LogContext`` builder that attaches unified fields
(trace_id, session_id, message_id, agent_name, elapsed_ms) to log messages
without requiring a full tracing framework.
"""

from __future__ import annotations

import time
from typing import Any


class LogContext:
    """Builder for observability log entries with unified key fields."""

    __slots__ = ("_fields", "_start_time")

    def __init__(self, **fields: Any) -> None:
        self._fields: dict[str, Any] = dict(fields)
        self._start_time: float | None = None

    def field(self, key: str, value: Any) -> LogContext:
        self._fields[key] = value
        return self

    def start(self) -> LogContext:
        self._start_time = time.monotonic()
        return self

    @property
    def elapsed_ms(self) -> float | None:
        if self._start_time is None:
            return None
        return (time.monotonic() - self._start_time) * 1000.0

    def to_extra(self) -> dict[str, Any]:
        extra: dict[str, Any] = dict(self._fields)
        if self._start_time is not None:
            extra["elapsed_ms"] = round(self.elapsed_ms, 1)  # type: ignore[arg-type]
        return extra

    def fmt(self, message: str) -> str:
        parts = [message]
        for key in ("session_id", "agent_name", "trace_id"):
            val = self._fields.get(key)
            if val is not None:
                parts.append(f"{key}={val}")
        elapsed = self.elapsed_ms
        if elapsed is not None:
            parts.append(f"elapsed={elapsed:.0f}ms")
        return " ".join(parts)
