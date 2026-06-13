"""TmuxPtyBackend — unified Unix backend using tmux + libtmux."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from framework.tools.terminal.backends.base import (
    TerminalBackend,
    extract_current_segment_from_buffer,
)
from framework.tools.terminal.prompt import is_prompt_ready
from framework.tools.terminal.pty_keys import CTRL_C
from framework.tools.terminal.results import TerminalRead, TerminalSegment
from framework.tools.terminal.types import Platform, TerminalVisibility

logger = logging.getLogger(__name__)

_DRAIN_TIMEOUT = 8.0
_DRAIN_POLL = 0.3


class TmuxPtyBackend(TerminalBackend):
    """Unix terminal backend using tmux.

    A single tmux session backs both headless and visible modes.
    Users attach via ``tmux attach -t <session>`` to see and interact
    with the same terminal the agent controls.
    """

    platform = Platform.LINUX
    visibility = TerminalVisibility.HIDDEN

    def __init__(self) -> None:
        try:
            import libtmux
        except ImportError as e:
            raise ImportError("libtmux is required on Unix. Install: pip install libtmux") from e
        self._libtmux = libtmux
        self._server: Any = libtmux.Server()
        self._session: Any = None
        self._pane: Any = None
        self._last_capture: str | None = None
        self._session_name: str | None = None
        self._shell: str | None = None

    @property
    def window_title(self) -> str:
        return self._session_name or "tmux"

    async def start(
        self,
        shell: str | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self._shell = shell or "/bin/sh"
        self._session_name = f"agent_{os.getpid()}_{id(self)}"

        loop = asyncio.get_running_loop()
        self._session = await loop.run_in_executor(
            None,
            lambda: self._server.new_session(
                session_name=self._session_name,
                attach=False,
                window_name="main",
                environment=env or {},
                start_directory=cwd,
            ),
        )
        window = self._session.windows[0]
        self._pane = window.panes[0]

        if self._shell != "/bin/sh":
            await loop.run_in_executor(
                None,
                lambda: self._pane.send_keys(self._shell, enter=True),
            )

        logger.info(
            "tmux session started: %s (shell=%s). Attach: tmux attach -t %s",
            self._session_name,
            self._shell,
            self._session_name,
        )

    async def drain_startup(self) -> None:
        """Poll capture_pane until a prompt appears."""
        loop = asyncio.get_running_loop()
        elapsed = 0.0
        while elapsed < _DRAIN_TIMEOUT:
            if not await self.is_alive():
                logger.warning("tmux session died during startup drain")
                return
            await asyncio.sleep(_DRAIN_POLL)
            elapsed += _DRAIN_POLL
            text = await loop.run_in_executor(None, lambda: "\n".join(self._pane.capture_pane()))
            if is_prompt_ready(text):
                self._last_capture = text
                logger.debug("tmux drain_startup: ready after %.1fs", elapsed)
                # Drain remaining startup output for readline shells.
                # Pager suppression is handled by PAGER=cat in build_full_env().
                if self._shell:
                    name = self._shell.lower()
                    if any(name.endswith(s) for s in ("bash", "zsh", "sh")):
                        await asyncio.sleep(0.2)
                        for _ in range(5):
                            await self.read(timeout=0.2, max_size=65536)
                return
        logger.warning("tmux drain_startup: timed out after %.0fs", _DRAIN_TIMEOUT)
        self._last_capture = ""

    async def write(self, data: str) -> None:
        if self._pane is None:
            raise RuntimeError("tmux session not started")
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, lambda: self._pane.send_keys(data, enter=False))

    async def read(self, timeout: float = 5.0, max_size: int = 65536) -> str:
        if self._pane is None:
            raise RuntimeError("tmux session not started")
        await asyncio.sleep(min(timeout, 0.5))

        loop = asyncio.get_running_loop()
        current = await loop.run_in_executor(None, lambda: "\n".join(self._pane.capture_pane()))

        if self._last_capture is None:
            self._last_capture = current
            return ""

        new_content = self._diff_output(self._last_capture, current)
        self._last_capture = current
        return new_content[:max_size]

    def _diff_output(self, previous: str, current: str) -> str:
        """Return new content from *current* that was not in *previous*."""
        prev_lines = previous.splitlines()
        curr_lines = current.splitlines()

        match_len = 0
        max_match = min(len(prev_lines), len(curr_lines))
        for i in range(1, max_match + 1):
            if prev_lines[-i] == curr_lines[-i]:
                match_len = i
            else:
                break

        if match_len == 0:
            new_lines = curr_lines
        else:
            new_lines = curr_lines[: len(curr_lines) - match_len]

        return "\n".join(new_lines)

    async def is_alive(self) -> bool:
        if self._session_name is None:
            return False
        loop = asyncio.get_running_loop()
        try:
            sessions = await loop.run_in_executor(None, lambda: self._server.sessions)
            return any(s.name == self._session_name for s in sessions)
        except Exception:
            return False

    async def terminate(self) -> None:
        if self._session is not None:
            loop = asyncio.get_running_loop()
            try:
                await loop.run_in_executor(None, self._session.kill_session)
            except Exception:
                pass
            self._session = None
            self._pane = None

    async def kill(self) -> None:
        await self.terminate()

    async def clear_input_line(self) -> None:
        """Clear current input line for readline shells."""
        if self._shell:
            name = self._shell.lower()
            if any(name.endswith(s) for s in ("bash", "zsh", "sh")):
                await self.write("\x01\x0b")

    async def read_pending(self, timeout: float = 5.0, max_size: int = 65536) -> TerminalRead:
        raw = await self.read(timeout=timeout, max_size=max_size)
        return TerminalRead(stdout=raw, raw=raw)

    async def current_segment(self) -> TerminalSegment:
        if self._pane is None:
            return TerminalSegment(text="")
        loop = asyncio.get_running_loop()
        text = await loop.run_in_executor(None, lambda: "\n".join(self._pane.capture_pane()))
        return extract_current_segment_from_buffer(text)

    async def interrupt(self) -> None:
        await self.write(CTRL_C)

    def stdin_writable(self) -> bool:
        return self._pane is not None

    def output_buffer_text(self) -> str:
        """Return the last captured pane text."""
        return self._last_capture or ""
