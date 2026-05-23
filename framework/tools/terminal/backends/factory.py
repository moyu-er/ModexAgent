"""Cross-platform PTY backend factory."""

from __future__ import annotations

import logging
import sys

from .base import TerminalBackend

logger = logging.getLogger(__name__)


def create_pty_backend() -> TerminalBackend:
    """Create the appropriate PTY backend for the current platform.

    Raises:
        ImportError: If the required platform library is not installed.
    """
    if sys.platform == "win32":
        try:
            from .windows_pty import WindowsPtyBackend
            return WindowsPtyBackend()
        except ImportError as e:
            logger.error("pywinpty not installed. Install with: pip install pywinpty")
            raise
    else:
        try:
            from .unix_pty import UnixPtyBackend
            return UnixPtyBackend()
        except ImportError as e:
            logger.error("pexpect not installed. Install with: pip install pexpect")
            raise
