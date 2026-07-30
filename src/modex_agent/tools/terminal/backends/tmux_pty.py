"""TmuxPtyBackend — unified Unix backend using tmux + libtmux.

ADR-0010 Decision 4 (switch branch): visibility is a constructor parameter,
not a subclass split. tmux's ``new_session(attach=...)`` is the only
difference between HIDDEN and VISIBLE — the I/O architecture is otherwise
identical (same pane capture, same send_keys path).

ADR-0010 Decision 6: Linux-visible MVP is ``TmuxBackend(visibility=VISIBLE)``.
Default remains HIDDEN for backwards compatibility.

ADR-0032 D5: this backend is a **snapshot backend** — it overrides
``write`` / ``read`` / ``read_pending`` / ``current_segment`` /
``drain_startup`` directly (no ``_write_blocking`` / ``_read_blocking``
hooks) because tmux's I/O model is a control-protocol snapshot
(``send_keys`` writes; ``capture_pane`` reads a pane snapshot), not a
byte stream. Forcing tmux into byte-stream shape via ``pipe-pane`` was
rejected as over-convergence (ADR-0032 D5 Considered Options).

D4 convergence: ``_shell_family`` is implemented; ``clear_input_line`` is
inherited from the base class (the base's ``"\\x01\\x0b"`` write path
works for tmux because ``write`` is overridden to call
``pane.send_keys(data, enter=False)``); the inline shell-suffix tuple
previously duplicated in ``clear_input_line`` and ``drain_startup`` is
deleted; the ``_uses_readline`` private helper is absent.

Fix 1 (correctness): ``read`` switches from ``capture_pane()`` (visible
window only) to ``capture_pane("-S", "-")`` (full scrollback, default
2000 lines). ``_diff_output`` switches from tail-matching to prefix-
matching: returns the suffix of *current* that follows the *previous*
snapshot's lines when *previous* is a line-prefix of *current*; falls
back to the entire *current* snapshot on prefix-match failure (output
scrolled beyond the 2000-line scrollback between two ``capture_pane``
calls). The previous tail-match algorithm returned the entire current
snapshot as "new" output whenever the pane scrolled past the visible
window, producing duplicates on every >30-line command.

Fix 2 (performance): ``is_alive`` caches the session-existence check
result for 1 second (``_alive_cache: tuple[float, bool] | None``). The
poll loop calls ``is_alive`` ~20×/s; without the cache each call would
spawn a ``tmux ls`` subprocess via ``run_in_executor``. The cache is
invalidated on ``terminate`` / ``kill``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import subprocess
import time
from typing import Any

from modex_agent.tools.terminal.backends.base import (
    TerminalBackend,
    extract_current_segment_from_buffer,
)
from modex_agent.tools.terminal.prompt import is_prompt_ready
from modex_agent.tools.terminal.pty_keys import CTRL_C
from modex_agent.tools.terminal.results import TerminalRead, TerminalSegment
from modex_agent.tools.terminal.types import (
    Platform,
    ShellFamily,
    TerminalVisibility,
    _family_from_path,
)

logger = logging.getLogger(__name__)

_DRAIN_TIMEOUT = 8.0
_DRAIN_POLL = 0.3
_ALIVE_CACHE_TTL_S = 1.0


class TmuxPtyBackend(TerminalBackend):
    """Unix terminal backend using tmux.

    ADR-0032 D5 snapshot backend — see module docstring.
    """

    platform = Platform.LINUX

    def __init__(self, *, visibility: TerminalVisibility = TerminalVisibility.HIDDEN) -> None:
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
        self._visibility = visibility
        self._alive_cache: tuple[float, bool] | None = None

    @property
    def visibility(self) -> TerminalVisibility:
        return self._visibility

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

        def _create_session():
            self._server.set_option("history-limit", 5000)
            return self._server.new_session(
                session_name=self._session_name,
                attach=False,
                window_name="main",
                window_command=self._shell,
                environment=env or {},
                start_directory=cwd,
            )

        self._session = await loop.run_in_executor(None, _create_session)
        window = self._session.windows[0]
        self._pane = window.panes[0]

        if self._visibility == TerminalVisibility.VISIBLE:
            await loop.run_in_executor(None, self._open_visible_window)

        logger.info(
            "tmux session started: %s (shell=%s, visible=%s). Attach: tmux attach -t %s",
            self._session_name,
            self._shell,
            self._visibility == TerminalVisibility.VISIBLE,
            self._session_name,
        )

    def _open_visible_window(self) -> None:
        """Open a terminal emulator window running ``tmux attach``.

        The tmux session is created detached; this spawns a new
        Terminal.app / xterm / gnome-terminal window that attaches to
        it. The user sees the real shell and can interact directly
        (keyboard, Ctrl-C, Tab completion) — tmux shares the PTY
        between the visible client and the agent's control protocol.
        """
        attach_cmd = f"exec tmux attach -t {self._session_name}"
        if shutil.which("osascript"):
            subprocess.Popen(
                ["osascript", "-e", f'tell application "Terminal" to do script "{attach_cmd}"'],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        elif shutil.which("xterm"):
            subprocess.Popen(
                ["xterm", "-e", "tmux", "attach", "-t", self._session_name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        elif shutil.which("gnome-terminal"):
            subprocess.Popen(
                ["gnome-terminal", "--", "tmux", "attach", "-t", self._session_name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            logger.warning(
                "VISIBLE tmux: no terminal emulator found. Session %s is detached — "
                "attach manually: tmux attach -t %s",
                self._session_name,
                self._session_name,
            )

    def _close_visible_window(self) -> None:
        """Close the Terminal.app window opened for this session.

        With ``exec tmux attach``, killing the tmux session causes
        ``tmux attach`` to exit. The Terminal.app window is left idle
        (title no longer contains ``-zsh``/``-bash``/``tmux``).
        ``Terminal close`` on such a window may trigger a confirmation
        dialog, so we use System Events to click the close button,
        which bypasses the dialog.

        Silently does nothing on failure — the tmux session is already
        killed, so the visible window is a cosmetic leftover.
        """
        if not shutil.which("osascript"):
            return
        script = (
            'tell application "System Events"\n'
            'tell process "Terminal"\n'
            "set toClose to {}\n"
            "repeat with w in windows\n"
            "set winName to name of w\n"
            'if winName does not contain "-zsh" and winName does not contain "-bash" and winName does not contain "login" then\n'
            "set end of toClose to w\n"
            "end if\n"
            "end repeat\n"
            "repeat with w in toClose\n"
            "click button 1 of w\n"
            "end repeat\n"
            "end tell\n"
            "end tell"
        )
        with contextlib.suppress(Exception):
            subprocess.run(
                ["osascript", "-e", script],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )

    async def drain_startup(self) -> None:
        """Poll capture_pane until a prompt appears.

        ADR-0032 D5: this override is retained because tmux's snapshot
        I/O model requires ``capture_pane``-based prompt detection
        rather than the byte-stream ``drain_windows_startup`` helper
        the base class uses.
        """
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
                if self._shell_family().uses_readline():
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
        # ADR-0032 D5 Fix 1: capture full scrollback (``-S -``) instead
        # of just the visible window. The default visible-window
        # snapshot shifts when the pane scrolls, breaking tail-match
        # diffing on every >30-line command. Full scrollback gives the
        # prefix-match diff a stable, larger window to match against.
        current = await loop.run_in_executor(
            None, lambda: "\n".join(self._pane.capture_pane("-S", "-"))
        )

        if self._last_capture is None:
            self._last_capture = current
            return ""

        new_content = self._diff_output(self._last_capture, current)
        self._last_capture = current
        return new_content[:max_size]

    def _diff_output(self, previous: str, current: str) -> str:
        """Return new content from *current* that was not in *previous*.

        Handles two cases:
        1. **Pure append** — previous is a line-prefix of current (output
           appended at the bottom, scrollback stable). Returns the suffix.
        2. **Last-line modified** — the prompt line in previous got the
           command text appended (``$ `` → ``$ echo FIRST``). All lines
           except the last of previous match; returns from the first
           differing line in current.
        Falls back to the entire current snapshot when neither matches.
        """
        prev_lines = previous.splitlines()
        curr_lines = current.splitlines()

        if len(prev_lines) <= len(curr_lines) and prev_lines == curr_lines[: len(prev_lines)]:
            return "\n".join(curr_lines[len(prev_lines) :])

        if len(prev_lines) >= 1:
            head = prev_lines[:-1]
            if len(head) <= len(curr_lines) and head == curr_lines[: len(head)]:
                return "\n".join(curr_lines[len(head) :])

        return "\n".join(curr_lines)

    async def is_alive(self) -> bool:
        if self._session_name is None:
            return False
        # 1-second TTL cache (ADR-0032 D5 Fix 2). The poll loop calls
        # ``is_alive`` ~20×/s; without the cache each call would spawn
        # a ``tmux ls`` subprocess via ``run_in_executor``.
        if self._alive_cache is not None:
            cached_at, cached_bool = self._alive_cache
            if time.monotonic() - cached_at < _ALIVE_CACHE_TTL_S:
                return cached_bool
        loop = asyncio.get_running_loop()
        try:
            sessions = await loop.run_in_executor(None, lambda: self._server.sessions)
            result = any(s.name == self._session_name for s in sessions)
        except Exception:
            result = False
        self._alive_cache = (time.monotonic(), result)
        return result

    async def terminate(self) -> None:
        loop = asyncio.get_running_loop()
        if self._session is not None:
            try:
                kill_fn = getattr(self._session, "kill", None) or self._session.kill_session
                await loop.run_in_executor(None, kill_fn)
            except Exception as exc:
                logger.warning("tmux terminate failed: %s", exc)
            self._session = None
            self._pane = None
        if self._visibility == TerminalVisibility.VISIBLE:
            await loop.run_in_executor(None, self._close_visible_window)
        self._alive_cache = None

    async def kill(self) -> None:
        await self.terminate()

    async def read_pending(self, timeout: float = 5.0, max_size: int = 65536) -> TerminalRead:
        raw = await self.read(timeout=timeout, max_size=max_size)
        return TerminalRead(stdout=raw, raw=raw)

    async def current_segment(self) -> TerminalSegment:
        if self._pane is None:
            return TerminalSegment(text="")
        loop = asyncio.get_running_loop()
        text = await loop.run_in_executor(None, lambda: "\n".join(self._pane.capture_pane()))
        return extract_current_segment_from_buffer(text)

    def _shell_family(self) -> ShellFamily:
        """Return the shell family of the running shell (ADR-0032 D4.1)."""
        return _family_from_path(self._shell or "")

    async def interrupt(self) -> None:
        await self.write(CTRL_C)

    def stdin_writable(self) -> bool:
        return self._pane is not None

    def output_buffer_text(self) -> str:
        """Return the last captured pane text."""
        return self._last_capture or ""

    def clear_buffer(self) -> None:
        """No-op for snapshot backends — tmux uses _last_capture, not _output_buffer."""
        pass
