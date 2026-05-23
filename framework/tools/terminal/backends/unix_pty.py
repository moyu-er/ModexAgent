"""Unix PTY backend — thin wrapper around pexpect."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from .base import TerminalBackend

logger = logging.getLogger(__name__)


class UnixPtyBackend(TerminalBackend):
    """Unix PTY using pexpect.

    Core code < 35 lines. All PTY protocol details handled by pexpect.
    pexpect.read_nonblocking is non-blocking, naturally suits async wrapping.

    EXTENSION: Phase 2+ visible windows:
      - Add `visible: bool` parameter.
      - visible=True: spawn xterm -e bash instead of direct bash spawn.
    """

    def __init__(self):
        try:
            import pexpect
        except ImportError as e:
            raise ImportError(
                "pexpect is required for Unix PTY. Install: pip install pexpect"
            ) from e
        self._pexpect = pexpect
        self._child: Any | None = None

    async def start(
        self,
        shell: str | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        shell = shell or "/bin/sh"
        loop = asyncio.get_event_loop()
        self._child = await loop.run_in_executor(
            None,
            lambda: self._pexpect.spawn(shell, cwd=cwd, env=env, encoding="utf-8"),
        )
        logger.debug("Unix PTY started: %s", shell)

    async def write(self, data: str) -> None:
        if self._child is None:
            raise RuntimeError("PTY not started")
        self._child.send(data)

    async def read(self, timeout: float = 5.0, max_size: int = 65536) -> str:
        if self._child is None:
            raise RuntimeError("PTY not started")
        try:
            return self._child.read_nonblocking(size=max_size, timeout=timeout)
        except self._pexpect.TIMEOUT:
            return ""
        except self._pexpect.EOF:
            return ""

    async def is_alive(self) -> bool:
        if self._child is None:
            return False
        return self._child.isalive()

    async def terminate(self) -> None:
        if self._child is not None:
            self._child.terminate()

    async def kill(self) -> None:
        if self._child is not None:
            self._child.kill(9)
