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
import logging
import os
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
        # 1-second TTL cache for ``is_alive`` (ADR-0032 D5 Fix 2). The
        # poll loop calls ``is_alive`` ~20×/s; without the cache each
        # call would spawn a ``tmux ls`` subprocess via ``run_in_executor``.
        # ``None`` means "no cached value; next call must re-query".
        self._alive_cache: tuple[float, bool] | None = None

    @property
    def visibility(self) -> TerminalVisibility:
        return self._visibility

    @property
    def _attach(self) -> bool:
        """Whether ``new_session(attach=...)`` attaches the new session to a window."""
        return self._visibility == TerminalVisibility.VISIBLE

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
                attach=self._attach,
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
            "tmux session started: %s (shell=%s, attach=%s). Attach: tmux attach -t %s",
            self._session_name,
            self._shell,
            self._attach,
            self._session_name,
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

        ADR-0032 D5 Fix 1: prefix-match algorithm. If *previous* is a
        line-prefix of *current* (the common case under
        ``capture-pane -p -S -`` — output is appended at the bottom and
        scrollback is stable), return the suffix as new output.
        Prefix-match failure (rare: output scrolls beyond the 2000-line
        scrollback between two ``capture_pane`` calls, or content
        changed completely) falls back to the entire *current* snapshot
        — same as the previous tail-match behavior, but only in the
        genuine edge case rather than on every >30-line command.

        Replaces the tail-match algorithm (which returned the entire
        current snapshot as "new" output whenever the pane scrolled
        past the visible window, producing duplicates).
        """
        prev_lines = previous.splitlines()
        curr_lines = current.splitlines()
        # Prefix-match: previous is a line-prefix of current.
        if len(prev_lines) <= len(curr_lines) and prev_lines == curr_lines[: len(prev_lines)]:
            new_lines = curr_lines[len(prev_lines) :]
            return "\n".join(new_lines)
        # Prefix-match failure — fall back to entire current snapshot.
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
        if self._session is not None:
            loop = asyncio.get_running_loop()
            try:
                await loop.run_in_executor(None, self._session.kill_session)
            except Exception as exc:
                logger.debug("tmux terminate failed: %s", exc)
            self._session = None
            self._pane = None
        # Invalidate the ``is_alive`` cache (ADR-0032 D5 Fix 2). The
        # session was killed; the next ``is_alive`` call must re-query
        # (and will return False because the session is gone).
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
