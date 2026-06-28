"""Hidden Windows PTY backend — in-process pywinpty with no visible console window.

Uses pywinpty.PtyProcess directly (no helper subprocess, no TCP socket, no
CREATE_NEW_CONSOLE).  The PTY runs entirely headless.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import sys

from modex_agent.tools.terminal.prompt import drain_windows_startup
from modex_agent.tools.terminal.results import SlidingOutputBuffer, TerminalRead, TerminalSegment
from modex_agent.tools.terminal.types import Platform, TerminalVisibility, _family_from_path

from .base import TerminalBackend, extract_current_segment_from_buffer
from .winpty import WinptyBackend

logger = logging.getLogger(__name__)


class WinptyHiddenBackend(WinptyBackend):
    """WinptyHiddenBackend — Windows hidden in-process winpty backend.

    Renamed per ADR-0010 Decision 3. The legacy name
    ``WindowsHiddenPtyBackend`` is re-exported as a deprecated alias in
    ``backends/__init__.py`` for the migration window.
    """

    platform = Platform.WINDOWS
    visibility = TerminalVisibility.HIDDEN

    def __init__(self) -> None:
        super().__init__()
        self._proc: object | None = None
        self._shell: str | None = None
        self._output_buffer = SlidingOutputBuffer()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(
        self,
        shell: str | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        if sys.platform != "win32":
            raise RuntimeError("WinptyHiddenBackend requires Windows")

        if shell is None:
            bash = shutil.which("bash")
            shell = bash if bash else "cmd.exe"
        self._shell = shell

        import winpty

        # Pass argv as a single-element list so paths containing spaces
        # (e.g. "C:\Program Files\Git\bin\bash.exe") are not split by shlex.
        self._proc = winpty.PtyProcess.spawn(
            [shell],
            cwd=cwd,
            env=env,
            dimensions=(30, 120),
        )
        logger.debug("Windows hidden PTY started: %s", shell)

    async def write(self, data: str) -> None:
        if self._proc is None:
            raise RuntimeError("PTY not started")
        self._proc.write(data)  # type: ignore[union-attr]

    async def read(self, timeout: float = 5.0, max_size: int = 65536) -> str:
        """Read raw output without buffering (matching WinptyConsoleWindowBackend).

        drain_startup() / drain_windows_startup() call read(), not
        read_pending(), so this keeps startup output out of the buffer.
        """
        if self._proc is None:
            raise RuntimeError("PTY not started")
        loop = asyncio.get_running_loop()

        def _do_read() -> str:
            fobj = self._proc.fileobj  # type: ignore[union-attr]
            fobj.settimeout(timeout)
            try:
                raw = fobj.recv(max_size)
                return raw.decode("utf-8", errors="replace")
            except (TimeoutError, OSError):
                return ""

        try:
            return await loop.run_in_executor(None, _do_read)
        except Exception:
            return ""

    async def read_pending(self, timeout: float = 5.0, max_size: int = 65536) -> TerminalRead:
        if self._proc is None:
            return TerminalRead(stdout="", raw="")

        loop = asyncio.get_running_loop()

        def _do_read() -> str:
            # fileobj is a socket.socket — use settimeout + recv for timed read
            fobj = self._proc.fileobj  # type: ignore[union-attr]
            fobj.settimeout(timeout)
            try:
                raw = fobj.recv(max_size)
                return raw.decode("utf-8", errors="replace")
            except (TimeoutError, OSError):
                return ""

        try:
            raw = await loop.run_in_executor(None, _do_read)
            if raw:
                self._append_to_buffer(raw)
            return TerminalRead(stdout=raw, raw=raw)
        except Exception:
            return TerminalRead(stdout="", raw="")

    async def current_segment(self) -> TerminalSegment:
        assert self._output_buffer is not None
        return extract_current_segment_from_buffer(self._output_buffer.text)

    async def interrupt(self) -> None:
        if self._proc is None:
            raise RuntimeError("PTY not started")
        self._proc.sendintr()  # type: ignore[union-attr]

    def stdin_writable(self) -> bool:
        return self._proc is not None

    async def is_alive(self) -> bool:
        if self._proc is None:
            return False
        try:
            return self._proc.isalive()  # type: ignore[union-attr]
        except Exception:
            return False

    async def terminate(self) -> None:
        if self._proc is not None:
            try:
                self._proc.terminate(force=False)  # type: ignore[union-attr]
            except Exception:
                pass
            self._proc = None

    async def kill(self) -> None:
        if self._proc is not None:
            try:
                self._proc.terminate(force=True)  # type: ignore[union-attr]
            except Exception:
                pass
            self._proc = None

    def _uses_readline(self) -> bool:
        if not self._shell:
            return True  # safe default for unknown shell
        return _family_from_path(self._shell).uses_readline()

    async def drain_startup(self) -> None:
        """Consume startup output; clear readline line only for bash/zsh."""
        await drain_windows_startup(
            read_fn=self.read,
            write_fn=self.write,
            is_alive_fn=self.is_alive,
            uses_readline=self._uses_readline(),
        )

    async def clear_input_line(self) -> None:
        """Clear current input line for readline shells; no-op for cmd."""
        if self._uses_readline():
            await self.write("\x01\x0b")
