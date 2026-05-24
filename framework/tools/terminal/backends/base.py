"""TerminalBackend abstract base class.

Implementations:
- VisibleWindowsPtyBackend: Windows subprocess with CREATE_NEW_CONSOLE
- TmuxPtyBackend: Unix tmux + libtmux
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class TerminalBackend(ABC):
    """Abstract terminal backend — wraps platform-specific PTY libraries."""

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
    async def read(self, timeout: float = 5.0, max_size: int = 65536) -> str:
        """Read PTY output. Non-blocking; returns collected text on timeout."""

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

    @property
    def window_title(self) -> str | None:
        """Human-readable OS window title when the backend has one."""
        return None
