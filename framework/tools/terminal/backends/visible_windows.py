"""Visible Windows PTY backend — subprocess with CREATE_NEW_CONSOLE.

Launches a helper process (visible_windows_host.py) that owns a visible
console window.  The helper creates a winpty.PtyProcess and forwards I/O
via a local TCP socket so the parent process and the visible window share
the exact same data stream.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

from framework.tools.terminal.prompt import drain_windows_startup
from framework.tools.terminal.results import SlidingOutputBuffer, TerminalRead, TerminalSegment
from framework.tools.terminal.types import Platform, TerminalVisibility, _family_from_path

from .base import TerminalBackend, extract_current_segment_from_buffer

logger = logging.getLogger(__name__)
_READ_TIMEOUT = 0.5


class VisibleWindowsPtyBackend(TerminalBackend):
    """Windows visible terminal using a subprocess with CREATE_NEW_CONSOLE.

    The helper process owns the visible console window and a PtyProcess.
    I/O is forwarded through a local TCP socket so the data stream seen
    by the parent (TerminalSession) is identical to what appears in the
    visible window.
    """

    platform = Platform.WINDOWS
    visibility = TerminalVisibility.VISIBLE

    def __init__(self) -> None:
        super().__init__()
        self._proc: subprocess.Popen | None = None
        self._sock: socket.socket | None = None
        self._shell: str | None = None
        self._title: str = "agent-terminal"
        self._output_buffer = SlidingOutputBuffer()

    @property
    def window_title(self) -> str:
        return self._title

    async def start(
        self,
        shell: str | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        if shell is None:
            bash = shutil.which("bash")
            shell = bash if bash else "cmd.exe"
        self._shell = shell
        self._title = f"Agent: {self._shell}"

        # Create a local socket server (parent = server, helper = client)
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", 0))
        _, port = server.getsockname()
        server.listen(1)
        server.settimeout(10.0)

        host_script = Path(__file__).parent / "visible_windows_host.py"
        cmd = [sys.executable, str(host_script), self._shell, str(port)]
        if cwd:
            cmd.append(cwd)
        self._proc = subprocess.Popen(
            cmd,
            creationflags=subprocess.CREATE_NEW_CONSOLE,
            cwd=cwd,
            env=env,
        )

        loop = asyncio.get_running_loop()

        def _accept() -> tuple[socket.socket, Any]:
            return server.accept()

        try:
            self._sock, _ = await loop.run_in_executor(None, _accept)
            self._sock.settimeout(_READ_TIMEOUT)
        except socket.timeout:
            self._proc.kill()
            raise RuntimeError(
                "Visible terminal host process did not connect within 10s"
            ) from None
        finally:
            server.close()

        logger.debug("Windows visible terminal started: %s", self._shell)

    async def write(self, data: str) -> None:
        if self._sock is None:
            raise RuntimeError("PTY not started")
        self._sock.sendall(data.encode("utf-8"))

    async def read(self, timeout: float = 5.0, max_size: int = 65536) -> str:
        if self._sock is None:
            raise RuntimeError("PTY not started")
        self._sock.settimeout(timeout)
        try:
            raw = self._sock.recv(max_size)
            return raw.decode("utf-8", errors="replace")
        except socket.timeout:
            return ""

    async def read_pending(
        self, timeout: float = 5.0, max_size: int = 65536
    ) -> TerminalRead:
        raw = await self.read(timeout=timeout, max_size=max_size)
        if raw:
            self._append_to_buffer(raw)
        return TerminalRead(stdout=raw, raw=raw)

    async def current_segment(self) -> TerminalSegment:
        assert self._output_buffer is not None
        return extract_current_segment_from_buffer(self._output_buffer.text)

    async def interrupt(self) -> None:
        await self.write("\x03")

    def stdin_writable(self) -> bool:
        return self._sock is not None

    async def is_alive(self) -> bool:
        if self._proc is None:
            return False
        return self._proc.poll() is None

    async def terminate(self) -> None:
        if self._sock is not None:
            self._sock.close()
            self._sock = None
        if self._proc is not None:
            self._proc.terminate()

    async def kill(self) -> None:
        if self._sock is not None:
            self._sock.close()
            self._sock = None
        if self._proc is not None:
            self._proc.kill()

    async def drain_startup(self) -> None:
        """Consume startup output then suppress pagers for readline shells."""
        is_bash = self._shell and "bash" in self._shell.lower()
        await drain_windows_startup(
            read_fn=self.read,
            write_fn=self.write,
            is_alive_fn=self.is_alive,
            uses_readline=bool(is_bash),
        )
        # Suppress interactive pagers for bash
        if is_bash:
            # TODEL await self.write("export GIT_PAGER=cat PAGER=cat LESS=FRX\n")
            await asyncio.sleep(0.3)
            for _ in range(5):
                await self.read(timeout=0.2, max_size=65536)

    async def clear_input_line(self) -> None:
        """Clear current input line for readline shells; no-op for cmd."""
        if self._shell and "bash" in self._shell.lower():
            await self.write("\x01\x0b")
