"""Factory for creating platform-appropriate PTY backends."""

from __future__ import annotations

import logging
import sys

from .base import TerminalBackend

logger = logging.getLogger(__name__)


def create_pty_backend() -> TerminalBackend:
    """Create a visible PTY backend for the current platform.

    Windows: VisibleWindowsPtyBackend.
    Linux/macOS: PexpectPtyBackend (preferred), TmuxPtyBackend (fallback).

    Raises:
        ImportError: If neither pexpect nor libtmux is installed on Unix.
    """
    if sys.platform == "win32":
        from .visible_windows import VisibleWindowsPtyBackend

        return VisibleWindowsPtyBackend()

    # Linux/macOS: pexpect preferred, tmux fallback
    try:
        import pexpect  # noqa: F401 — verify pexpect is installed

        from .pexpect_pty import PexpectPtyBackend

        return PexpectPtyBackend()
    except ImportError:
        logger.debug("pexpect not available, falling back to tmux")

    from .tmux_pty import TmuxPtyBackend

    return TmuxPtyBackend()
