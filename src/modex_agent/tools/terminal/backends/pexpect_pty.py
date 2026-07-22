"""PexpectPtyBackend — Linux/macOS hidden PTY backend using pexpect.

In-process PTY with no visible window.  Uses pexpect.spawn() for
pseudo-terminal management.  Modeled on WinptyHiddenBackend (legacy alias:
``WindowsHiddenPtyBackend``) for behavioral consistency (both are hidden,
in-process, third-party PTY).

ADR-0032 D3: this backend implements the two blocking-IO hooks
(``_write_blocking`` / ``_read_blocking``) plus the ``_shell_family`` hook.
The base-class ``write`` / ``read_pending`` / ``read`` / ``current_segment`` /
``clear_input_line`` / ``drain_startup`` template methods wrap the hooks in
``loop.run_in_executor`` and provide the shared byte-stream behaviors. The
six overrides and the ``_uses_readline`` private helper are deleted, which
structurally eliminates the synchronous-write-blocks-event-loop defect on
this path.
"""

from __future__ import annotations

import asyncio
import logging

from modex_agent.tools.terminal.results import SlidingOutputBuffer
from modex_agent.tools.terminal.types import (
    Platform,
    ShellFamily,
    TerminalVisibility,
    _family_from_path,
)

from .base import TerminalBackend

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
        self._proc: object | None = None  # pexpect.spawn
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
    # Async-safety contract hooks (ADR-0032 D1/D3)
    # ------------------------------------------------------------------
    # The base-class ``write`` / ``read_pending`` templates wrap these hooks
    # in ``loop.run_in_executor(None, ...)`` so blocking I/O is offloaded to
    # a worker thread and the event loop is not stalled when the PTY input
    # pipe is full (root cause 1, ADR-0032).

    def _write_blocking(self, data: str) -> None:
        """Blocking write hook — wrapped in ``run_in_executor`` by base ``write``."""
        if self._proc is None:
            raise RuntimeError("PTY not started")
        self._proc.send(data)  # type: ignore[attr-defined]

    def _read_blocking(self, timeout: float, max_size: int) -> str:
        """Blocking read hook — wrapped in ``run_in_executor`` by base ``read_pending``.

        ``proc.read_nonblocking`` blocks the calling thread until either
        output arrives or the pexpect timeout fires (handled as ``""``).
        The ``self._pexpect`` module is loaded by ``start()`` before any
        read; the guard refuses pre-start reads rather than
        ``AttributeError`` on ``None.exceptions``.
        """
        if self._proc is None or self._pexpect is None:
            raise RuntimeError("PTY not started")
        pexpect_mod = self._pexpect
        try:
            return self._proc.read_nonblocking(  # type: ignore[attr-defined,no-any-return]
                max_size, timeout=timeout
            )
        except pexpect_mod.exceptions.TIMEOUT:  # type: ignore[attr-defined]
            return ""
        except pexpect_mod.exceptions.EOF:  # type: ignore[attr-defined]
            return ""

    def _shell_family(self) -> ShellFamily:
        """Return the shell family of the running shell (ADR-0032 D4.1)."""
        return _family_from_path(self._shell or "")

    # ------------------------------------------------------------------
    # State queries
    # ------------------------------------------------------------------

    async def is_alive(self) -> bool:
        if self._proc is None:
            return False
        try:
            return self._proc.isalive()  # type: ignore[attr-defined,no-any-return]
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
        self._proc.sendintr()  # type: ignore[attr-defined]

    async def terminate(self) -> None:
        if self._proc is not None:
            loop = asyncio.get_running_loop()
            try:
                await loop.run_in_executor(
                    None,
                    lambda: self._proc.terminate(force=False),  # type: ignore[union-attr]
                )
            except Exception as exc:
                logger.debug("pexpect terminate failed: %s", exc)
            self._proc = None

    async def kill(self) -> None:
        if self._proc is not None:
            loop = asyncio.get_running_loop()
            try:
                await loop.run_in_executor(
                    None,
                    lambda: self._proc.terminate(force=True),  # type: ignore[union-attr]
                )
            except Exception as exc:
                logger.debug("pexpect kill failed: %s", exc)
            self._proc = None
