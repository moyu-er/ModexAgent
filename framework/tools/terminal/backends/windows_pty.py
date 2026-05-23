"""Windows PTY backend — thin wrapper around pywinpty.

Note: pip package name is 'pywinpty', but the importable module is 'winpty'.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from .base import TerminalBackend

logger = logging.getLogger(__name__)


class WindowsPtyBackend(TerminalBackend):
    """Windows PTY using pywinpty.

    Core code < 40 lines. All PTY protocol details handled by pywinpty.
    Synchronous pywinpty API is wrapped via asyncio.run_in_executor.

    EXTENSION: Phase 2+ visible windows:
      - pywinpty supports ConPTY visible mode via spawn flags.
      - Add `visible: bool` parameter to constructor.
    """

    def __init__(self):
        try:
            import winpty
        except ImportError as e:
            raise ImportError(
                "pywinpty is required for Windows PTY. Install: pip install pywinpty"
            ) from e
        self._winpty = winpty
        self._pty: Any | None = None

    async def start(
        self,
        shell: str | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        loop = asyncio.get_running_loop()
        shell = shell or "cmd.exe"
        self._pty = await loop.run_in_executor(
            None,
            lambda: self._winpty.PTY(80, 24),
        )
        await loop.run_in_executor(
            None,
            lambda: self._pty.spawn(shell, cwd=cwd, env=env),
        )
        logger.debug("Windows PTY started: %s", shell)

    async def write(self, data: str) -> None:
        if self._pty is None:
            raise RuntimeError("PTY not started")
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._pty.write, data)

    async def read(self, timeout: float = 5.0, max_size: int = 65536) -> str:
        if self._pty is None:
            raise RuntimeError("PTY not started")
        loop = asyncio.get_running_loop()
        try:
            return await asyncio.wait_for(
                loop.run_in_executor(None, self._pty.read, max_size),
                timeout=timeout,
            )
        except TimeoutError:
            return ""

    async def is_alive(self) -> bool:
        if self._pty is None:
            return False
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: self._pty.isalive())

    async def terminate(self) -> None:
        if self._pty is not None:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._pty.terminate)

    async def kill(self) -> None:
        if self._pty is not None:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._pty.kill)
