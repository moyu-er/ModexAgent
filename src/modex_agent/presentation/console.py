"""ConsolePresenter — the framework's reference terminal renderer.

A minimal :class:`PresentationSink` consumer proving the hub/sink seam
outside the bot project: text deltas append to the current line (thinking
runs open a prefixed line), each tool card renders as one line, an approval
request renders as a prompt line, and ``TurnFinished`` closes the turn with
a terminator line. Pure stdout via an injectable write function — no curses,
no ANSI escapes. Other kinds have no console surface and are ignored (the
transcript and wire consumers carry them).
"""

from __future__ import annotations

from collections.abc import Callable

from .events import (
    ApprovalRequested,
    PresentationEvent,
    TextDelta,
    ThinkingDelta,
    ToolCallStarted,
    ToolResult,
    TurnFinished,
)
from .hub import PresentationSink

_THINKING_PREFIX = "[thinking] "


class ConsolePresenter(PresentationSink):
    """Stream one turn's presentation events onto stdout (plain lines)."""

    def __init__(self, write: Callable[[str], None] | None = None) -> None:
        self._write = write or print
        self._line_open = False

    async def handle(self, event: PresentationEvent) -> None:
        match event:
            case TextDelta(text=text):
                self._append(text, prefix=None)
            case ThinkingDelta(text=text):
                self._append(text, prefix=_THINKING_PREFIX)
            case ToolCallStarted(tool_name=name, call_id=call_id):
                self._line(f"tool {name} [{call_id}] in_progress")
            case ToolResult(tool_name=name, call_id=call_id, error=error):
                status = "failed" if error else "completed"
                self._line(f"tool {name} [{call_id}] {status}")
            case ApprovalRequested(tool_name=name, call_id=call_id, prompt=prompt):
                self._line(f"approval needed: {name} [{call_id}] {prompt}")
            case TurnFinished(stop_reason=stop_reason, latency_ms=latency_ms):
                self._line(f"turn finished: {stop_reason.value} ({latency_ms} ms)")
            case _:
                # TurnStarted / TurnErrored / ApprovalResolved / UsageSummary /
                # ToolArgsDelta have no console line of their own.
                pass

    # ------------------------------------------------------------------
    # Line discipline (internal)
    # ------------------------------------------------------------------

    def _append(self, text: str, *, prefix: str | None) -> None:
        """Append a delta to the current line, opening it if needed."""
        if not self._line_open:
            self._write(prefix or "")
            self._line_open = True
        self._write(text)

    def _line(self, text: str) -> None:
        """Write one complete line, closing any open delta line first."""
        if self._line_open:
            self._write("\n")
            self._line_open = False
        self._write(text + "\n")


__all__ = ["ConsolePresenter"]
