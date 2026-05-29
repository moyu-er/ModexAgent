from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from framework.tools.terminal.types import ProcessStatus


@dataclass(frozen=True)
class TerminalRead:
    stdout: str = ""
    stderr: str = ""
    raw: str = ""


@dataclass(frozen=True)
class TerminalSegment:
    text: str
    cursor_line: str = ""
    is_empty_prompt: bool = False


@dataclass(frozen=True)
class CommandResult:
    status: ProcessStatus
    session_id: str | None
    terminal: str
    output: str
    tail: str
    pid: int | None = None
    cwd: str | None = None
    exit_code: int | None = None
    exit_signal: str | int | None = None
    timed_out: bool = False
    duration_ms: int | None = None
    failure_kind: str | None = None
    message: str | None = None
    started_at: float | None = None
    ended_at: float | None = None
    truncated: bool = False
    stdin_writable: bool | None = None
    waiting_for_input: bool | None = None
    idle_ms: int | None = None


@dataclass(frozen=True)
class ProcessActionResult:
    status: ProcessStatus
    session_id: str | None
    text: str
    details: dict[str, object]


class SlidingOutputBuffer:
    """Dual-constraint sliding window for terminal output.

    - Character constraint: total chars <= max_chars (default 200K)
    - Command constraint: keep last max_commands (default 100) command blocks
    - Both enforced simultaneously; whichever is stricter wins.
    """

    def __init__(self, max_chars: int = 200_000, max_commands: int = 100) -> None:
        self._command_chunks: deque[str] = deque(maxlen=max_commands)
        self._current_parts: list[str] = []
        self._total_chars = 0
        self._max_chars = max_chars

    def append(self, text: str) -> None:
        """Append output text to the current command's buffer."""
        self._current_parts.append(text)
        self._total_chars += len(text)
        self._trim_chars()

    def mark_command_boundary(self) -> None:
        """Seal current parts as a completed command block."""
        if self._current_parts:
            chunk = "".join(self._current_parts)
            self._command_chunks.append(chunk)
            self._current_parts = []
            self._recalc_total_chars()

    @property
    def text(self) -> str:
        """Reconstruct full buffer text from command chunks + current parts."""
        parts: list[str] = list(self._command_chunks)
        if self._current_parts:
            parts.append("".join(self._current_parts))
        return "".join(parts)

    @property
    def total_chars(self) -> int:
        """Total character count across all chunks and current parts."""
        return self._total_chars

    def clear(self) -> None:
        """Discard all buffered content."""
        self._command_chunks.clear()
        self._current_parts = []
        self._total_chars = 0

    def _trim_chars(self) -> None:
        """Remove oldest command chunks until total chars <= max_chars."""
        while self._total_chars > self._max_chars and self._command_chunks:
            removed = self._command_chunks.popleft()
            self._total_chars -= len(removed)

    def _recalc_total_chars(self) -> None:
        self._total_chars = sum(len(c) for c in self._command_chunks)
        self._total_chars += sum(len(p) for p in self._current_parts)
