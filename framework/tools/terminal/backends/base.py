"""TerminalBackend abstract base class.

Implementations:
- VisibleWindowsPtyBackend: Windows subprocess with CREATE_NEW_CONSOLE
- TmuxPtyBackend: Unix tmux + libtmux
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from framework.tools.terminal.results import SlidingOutputBuffer, TerminalRead, TerminalSegment
from framework.tools.terminal.types import Platform, TerminalVisibility


def extract_current_segment_from_buffer(text: str) -> TerminalSegment:
    """Extract the last terminal segment from buffered PTY output.

    Strips ANSI/CSI sequences before checking for prompt endings so that
    terminal control codes (e.g. \\x1b[0K, \\x1b[?25h) after the prompt
    do not prevent empty-prompt detection.

    Uses the same strict prompt detection as ``extract_last_command_output``
    (``is_prompt_ready`` + ``_is_prompt_with_command``) to avoid false
    positives where command output happens to end with ``$`` / ``>`` / ``#``.
    """
    from framework.tools.terminal.prompt import (
        _is_prompt_with_command,
        _strip_ansi_and_da1,
        is_prompt_ready,
    )

    clean = _strip_ansi_and_da1(text)
    lines = clean.splitlines()
    if not lines:
        return TerminalSegment(text="", cursor_line="", is_empty_prompt=True)
    cursor_line = lines[-1]
    prompt_indexes = [
        index
        for index, line in enumerate(lines)
        if is_prompt_ready(line) or _is_prompt_with_command(line)
    ]
    start = prompt_indexes[-1] if prompt_indexes else max(0, len(lines) - 1)
    segment_text = "\n".join(lines[start:])
    # is_empty_prompt: only ``is_prompt_ready`` — ``_is_prompt_with_command``
    # matches lines like "PS C:\\> npm install" which are *not* an empty
    # prompt (a command is already typed after the prompt).
    return TerminalSegment(
        text=segment_text,
        cursor_line=cursor_line,
        is_empty_prompt=is_prompt_ready(cursor_line),
    )


class TerminalBackend(ABC):
    """Abstract terminal backend — wraps platform-specific PTY libraries."""

    def __init__(self) -> None:
        self._output_buffer: SlidingOutputBuffer | None = None

    def mark_command_boundary(self) -> None:
        """Seal current buffer parts as a completed command block."""
        if self._output_buffer is not None:
            self._output_buffer.mark_command_boundary()

    def output_buffer_text(self) -> str:
        """Return the full output buffer text, or empty string if no buffer."""
        if self._output_buffer is not None:
            return self._output_buffer.text
        return ""

    def _append_to_buffer(self, text: str) -> None:
        """Append output text to the sliding output buffer."""
        if self._output_buffer is not None:
            self._output_buffer.append(text)

    @property
    @abstractmethod
    def platform(self) -> Platform:
        """Platform this backend runs on."""

    @property
    @abstractmethod
    def visibility(self) -> TerminalVisibility:
        """Whether the terminal window is visible to the user."""

    @abstractmethod
    async def start(
        self,
        shell: str | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        """Start the shell process."""

    @abstractmethod
    async def write(self, data: str) -> None:
        """Send input to the PTY."""

    @abstractmethod
    async def read_pending(
        self, timeout: float = 5.0, max_size: int = 65536
    ) -> TerminalRead:
        """Read pending PTY output as a TerminalRead struct."""

    async def read(self, timeout: float = 5.0, max_size: int = 65536) -> str:
        """Read PTY output. Non-blocking; returns collected text on timeout.

        Backward-compatible wrapper around read_pending().  Returns the raw
        string for callers that don't need the structured TerminalRead.
        """
        result = await self.read_pending(timeout=timeout, max_size=max_size)
        return result.raw

    @abstractmethod
    async def current_segment(self) -> TerminalSegment:
        """Snapshot the visible terminal content and cursor line."""

    @abstractmethod
    async def interrupt(self) -> None:
        """Send an interrupt signal (Ctrl-C) to the PTY."""

    @abstractmethod
    async def is_alive(self) -> bool:
        """Check if the shell process is still running."""

    @abstractmethod
    async def terminate(self) -> None:
        """Graceful termination (SIGTERM equivalent)."""

    @abstractmethod
    async def kill(self) -> None:
        """Force kill (SIGKILL equivalent)."""

    @abstractmethod
    async def drain_startup(self) -> None:
        """Wait until the terminal is ready for input (prompt visible).

        Called after start().  Default is a no-op; subclasses override
        to consume startup banners, ANSI sequences, etc.
        """

    @abstractmethod
    async def clear_input_line(self) -> None:
        """Clear the current input line without interrupting jobs.

        For readline shells this sends Ctrl+A Ctrl+K.
        For non-readline shells this is a no-op.
        """

    @abstractmethod
    def stdin_writable(self) -> bool:
        """Return True if stdin is currently writable."""

    @property
    def window_title(self) -> str | None:
        """Human-readable OS window title when the backend has one."""
        return None
