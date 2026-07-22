"""Hidden Windows PTY backend — in-process pywinpty with no visible console window.

Uses pywinpty.PtyProcess directly (no helper subprocess, no TCP socket, no
CREATE_NEW_CONSOLE).  The PTY runs entirely headless.

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

import logging
import shutil
import sys

from modex_agent.tools.terminal.results import SlidingOutputBuffer
from modex_agent.tools.terminal.types import (
    Platform,
    ShellFamily,
    TerminalVisibility,
    _family_from_path,
)

from .winpty_transport import WinptyBackend

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
        self._proc.write(data)  # type: ignore[attr-defined]

    def _read_blocking(self, timeout: float, max_size: int) -> str:
        """Blocking read hook — wrapped in ``run_in_executor`` by base ``read_pending``.

        ``settimeout`` mutates the pywinpty ``fileobj`` socket. Unlike the
        visible-windows TCP socket (ADR-0032 root cause 2), pywinpty's
        ``fileobj`` is a per-instance read-side socket — write goes through
        a different handle — so the mutation does not leak into write paths.
        """
        if self._proc is None:
            raise RuntimeError("PTY not started")
        fobj = self._proc.fileobj  # type: ignore[attr-defined]
        fobj.settimeout(timeout)
        try:
            raw: bytes = fobj.recv(max_size)
            return raw.decode("utf-8", errors="replace")
        except (TimeoutError, OSError):
            return ""

    def _shell_family(self) -> ShellFamily:
        """Return the shell family of the running shell (ADR-0032 D4.1)."""
        return _family_from_path(self._shell or "")

    # ------------------------------------------------------------------
    # Lifecycle continued
    # ------------------------------------------------------------------

    async def interrupt(self) -> None:
        if self._proc is None:
            raise RuntimeError("PTY not started")
        self._proc.sendintr()  # type: ignore[attr-defined]

    def stdin_writable(self) -> bool:
        return self._proc is not None

    async def is_alive(self) -> bool:
        if self._proc is None:
            return False
        try:
            return self._proc.isalive()  # type: ignore[attr-defined,no-any-return]
        except Exception:
            return False

    async def terminate(self) -> None:
        if self._proc is not None:
            try:
                self._proc.terminate(force=False)  # type: ignore[attr-defined]
            except Exception:
                pass
            self._proc = None

    async def kill(self) -> None:
        if self._proc is not None:
            try:
                self._proc.terminate(force=True)  # type: ignore[attr-defined]
            except Exception:
                pass
            self._proc = None
