"""PexpectPtyBackend — Linux/macOS hidden PTY backend using pexpect.

In-process PTY with no visible window.  Uses pexpect.spawn() for
pseudo-terminal management.  Modeled on WindowsHiddenPtyBackend for
behavioral consistency (both are hidden, in-process, third-party PTY).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time as _time

from framework.tools.terminal.prompt import drain_windows_startup
from framework.tools.terminal.results import SlidingOutputBuffer, TerminalRead, TerminalSegment
from framework.tools.terminal.types import Platform, ShellFamily, TerminalVisibility, _family_from_path

from .base import TerminalBackend, extract_current_segment_from_buffer

logger = logging.getLogger(__name__)


class PexpectPtyBackend(TerminalBackend):
    """Linux/macOS hidden terminal using pexpect in-process.

    No visible window.  The PTY lifecycle is managed entirely by pexpect.
    """

    platform = Platform.LINUX
    visibility = TerminalVisibility.HIDDEN

    def __init__(self) -> None:
        super().__init__()
        self._pexpect: object | None = None  # pexpect module, lazy-loaded in start()
        self._proc: object | None = None     # pexpect.spawn
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
        self._shell = shell or "/bin/sh"

        if self._pexpect is None:
            import pexpect as _pexpect_mod
            self._pexpect = _pexpect_mod

        loop = asyncio.get_running_loop()

        def _spawn() -> object:
            return self._pexpect.spawn(  # type: ignore[union-attr]
                self._shell,
                dimensions=(30, 120),
                cwd=cwd,
                env=env,
                encoding="utf-8",
                codec_errors="replace",
            )

        self._proc = await loop.run_in_executor(None, _spawn)
        logger.debug("pexpect PTY started: %s", self._shell)

    # ------------------------------------------------------------------
    # I/O
    # ------------------------------------------------------------------

    async def write(self, data: str) -> None:
        if self._proc is None:
            raise RuntimeError("PTY not started")
        self._proc.send(data)  # type: ignore[union-attr]

    async def read(self, timeout: float = 5.0, max_size: int = 65536) -> str:
        """Read raw output without buffering.

        drain_startup() calls read(), not read_pending(), so this keeps
        startup output out of the sliding buffer.
        """
        if self._proc is None:
            raise RuntimeError("PTY not started")
        loop = asyncio.get_running_loop()
        pexpect_mod = self._pexpect  # loaded in start(), always set before read()

        def _do_read() -> str:
            try:
                return self._proc.read_nonblocking(  # type: ignore[union-attr]
                    max_size, timeout=timeout
                )
            except pexpect_mod.exceptions.TIMEOUT:  # type: ignore[union-attr]
                return ""
            except pexpect_mod.exceptions.EOF:  # type: ignore[union-attr]
                return ""

        try:
            return await loop.run_in_executor(None, _do_read)
        except Exception:
            return ""

    async def read_pending(
        self, timeout: float = 5.0, max_size: int = 65536
    ) -> TerminalRead:
        raw = await self.read(timeout=timeout, max_size=max_size)
        if raw:
            self._append_to_buffer(raw)
        return TerminalRead(stdout=raw, raw=raw)

    # ------------------------------------------------------------------
    # State queries
    # ------------------------------------------------------------------

    async def current_segment(self) -> TerminalSegment:
        assert self._output_buffer is not None
        return extract_current_segment_from_buffer(self._output_buffer.text)

    async def is_alive(self) -> bool:
        if self._proc is None:
            return False
        try:
            return self._proc.isalive()  # type: ignore[union-attr]
        except Exception:
            return False

    def stdin_writable(self) -> bool:
        return self._proc is not None

    # ------------------------------------------------------------------
    # Signal / termination
    # ------------------------------------------------------------------

    async def interrupt(self) -> None:
        if self._proc is None:
            raise RuntimeError("PTY not started")
        self._proc.sendintr()  # type: ignore[union-attr]

    async def terminate(self) -> None:
        if self._proc is not None:
            loop = asyncio.get_running_loop()
            try:
                await loop.run_in_executor(
                    None, lambda: self._proc.terminate(force=False)  # type: ignore[union-attr]
                )
            except Exception:
                pass
            self._proc = None

    async def kill(self) -> None:
        if self._proc is not None:
            loop = asyncio.get_running_loop()
            try:
                await loop.run_in_executor(
                    None, lambda: self._proc.terminate(force=True)  # type: ignore[union-attr]
                )
            except Exception:
                pass
            self._proc = None

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------

    def _uses_readline(self) -> bool:
        if not self._shell:
            return True
        return _family_from_path(self._shell).uses_readline()

    async def drain_startup(self) -> None:
        """Consume startup output; reuse the generic PTY drain routine."""
        await drain_windows_startup(
            read_fn=self.read,
            write_fn=self.write,
            is_alive_fn=self.is_alive,
            uses_readline=self._uses_readline(),
        )

    async def clear_input_line(self) -> None:
        """Clear current input line for readline shells; no-op otherwise."""
        if self._uses_readline():
            await self.write("\x01\x0b")
