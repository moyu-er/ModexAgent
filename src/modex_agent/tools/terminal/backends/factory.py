"""Factory for creating platform-appropriate PTY backends.

ADR-0010: backend selection uses an explicit (transport, visibility) capability
table. Unsupported combinations raise ``UnsupportedVisibilityForTransport``
rather than silently falling back.

Transport × Visibility matrix:

  Windows (winpty transport):
    HIDDEN  → WinptyHiddenBackend (in-process)
    VISIBLE → WinptyConsoleWindowBackend (host process + CREATE_NEW_CONSOLE)

  POSIX (pty transport — native PTY via pexpect):
    HIDDEN  → PexpectPtyBackend (in-process pty.spawn)

  POSIX (tmux transport — Unix-only control protocol):
    HIDDEN  → TmuxPtyBackend(HIDDEN) (detached session)
    VISIBLE → TmuxPtyBackend(VISIBLE) (detached session + terminal window attach)

Factory priority on POSIX:
  HIDDEN:  pty transport (pexpect, no external binary) preferred → tmux fallback.
  VISIBLE: tmux transport (detached + terminal window attach).
"""

from __future__ import annotations

import logging
import shutil
import sys

from modex_agent.tools.terminal.backends.base import TerminalBackend
from modex_agent.tools.terminal.types import TerminalVisibility

logger = logging.getLogger(__name__)


class UnsupportedVisibilityForTransport(Exception):  # noqa: N818
    """The requested (transport, visibility) combination cannot be served."""


def _is_pexpect_available() -> bool:
    try:
        import pexpect  # noqa: F401

        return True
    except ImportError:
        return False


def _is_libtmux_available() -> bool:
    try:
        import libtmux  # noqa: F401
    except ImportError:
        return False
    return shutil.which("tmux") is not None


def _create_pexpect_backend() -> TerminalBackend:
    from .pexpect_pty import PexpectPtyBackend

    return PexpectPtyBackend()


def _create_tmux_backend(
    visibility: TerminalVisibility = TerminalVisibility.HIDDEN,
) -> TerminalBackend:
    from .tmux_pty import TmuxPtyBackend

    return TmuxPtyBackend(visibility=visibility)


def _create_winpty_hidden_backend() -> TerminalBackend:
    from .windows_hidden import WinptyHiddenBackend

    return WinptyHiddenBackend()


def _create_winpty_visible_backend() -> TerminalBackend:
    from .visible_windows import WinptyConsoleWindowBackend

    return WinptyConsoleWindowBackend()


def create_pty_backend(
    visibility: TerminalVisibility = TerminalVisibility.HIDDEN,
) -> TerminalBackend:
    """Create a PTY backend for the current platform with the requested visibility."""
    if sys.platform == "win32":
        if visibility == TerminalVisibility.VISIBLE:
            return _create_winpty_visible_backend()
        return _create_winpty_hidden_backend()

    # POSIX (Linux / macOS)
    if visibility == TerminalVisibility.VISIBLE:
        if not _is_libtmux_available():
            raise UnsupportedVisibilityForTransport(
                "No transport available for VISIBLE on this platform: "
                "libtmux + tmux binary required (tmux detached + terminal window attach)."
            )
        return _create_tmux_backend(visibility=TerminalVisibility.VISIBLE)

    # HIDDEN on POSIX
    if _is_pexpect_available():
        return _create_pexpect_backend()
    if _is_libtmux_available():
        return _create_tmux_backend(visibility=TerminalVisibility.HIDDEN)
    raise UnsupportedVisibilityForTransport(
        "No transport available for HIDDEN on this platform: "
        "install pexpect (`pip install pexpect`) or libtmux (`pip install libtmux`)."
    )
