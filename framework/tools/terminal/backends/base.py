"""TerminalBackend abstract base class.

Implementations:
- VisibleWindowsPtyBackend: Windows subprocess with CREATE_NEW_CONSOLE
- TmuxPtyBackend: Unix tmux + libtmux
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from framework.tools.terminal.results import TerminalRead, TerminalSegment
from framework.tools.terminal.types import Platform, TerminalVisibility


class TerminalBackend(ABC):
    """Abstract terminal backend — wraps platform-specific PTY libraries."""

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
