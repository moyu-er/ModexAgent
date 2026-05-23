"""TerminalBackend abstract base class.

EXTENSION: Phase 2+ visible windows do not need a new ABC.
  Add `visible: bool` parameter to PtyBackend subclasses.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class TerminalBackend(ABC):
    """Abstract terminal backend — wraps mature PTY libraries.

    Implementations:
    - WindowsPtyBackend: pywinpty wrapper
    - UnixPtyBackend: pexpect wrapper

    EXTENSION: Phase 2+
      - TmuxBackend(TerminalBackend): reuse tmux sessions
      - WindowBackend via visible=True on PtyBackend subclasses
    """

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
