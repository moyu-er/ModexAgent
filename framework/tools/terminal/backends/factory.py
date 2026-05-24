"""Factory for creating platform-appropriate PTY backends."""

from __future__ import annotations

import logging
import sys

from .base import TerminalBackend

logger = logging.getLogger(__name__)


def create_pty_backend() -> TerminalBackend:
    """Create a visible PTY backend for the current platform.

    Raises:
        ImportError: If the required platform library is not installed.
    """
    if sys.platform == "win32":
        from .visible_windows import VisibleWindowsPtyBackend
        return VisibleWindowsPtyBackend()

    from .tmux_pty import TmuxPtyBackend
    return TmuxPtyBackend()
