"""Factory for creating platform-appropriate PTY backends.

ADR-0010: backend selection uses an explicit (transport, visibility) capability
table. Unsupported combinations raise ``UnsupportedVisibilityForTransport``
rather than silently falling back.
"""

from __future__ import annotations

import logging
import sys
from typing import Callable

from modex_agent.tools.terminal.backends.base import TerminalBackend
from modex_agent.tools.terminal.types import TerminalVisibility

logger = logging.getLogger(__name__)


class UnsupportedVisibilityForTransport(Exception):
    """The requested (transport, visibility) combination cannot be served.

    Raised by ``create_pty_backend`` when the platform-preferred transport
    cannot realise the requested visibility and no alternative transport is
    available. Per ADR-0010 the factory must reject rather than fall back.
    """


def _is_pexpect_available() -> bool:
    try:
        import pexpect  # noqa: F401

        return True
    except ImportError:
        return False


def _is_libtmux_available() -> bool:
    try:
        import libtmux  # noqa: F401

        return True
    except ImportError:
        return False


def _create_pexpect_backend() -> TerminalBackend:
    from .pexpect_pty import PexpectPtyBackend

    return PexpectPtyBackend()


def _create_tmux_backend() -> TerminalBackend:
    from .tmux_pty import TmuxPtyBackend

    return TmuxPtyBackend()


def _create_winpty_hidden_backend() -> TerminalBackend:
    from .windows_hidden import WindowsHiddenPtyBackend

    return WindowsHiddenPtyBackend()


def _create_winpty_visible_backend() -> TerminalBackend:
    from .visible_windows import VisibleWindowsPtyBackend

    return VisibleWindowsPtyBackend()


def create_pty_backend(
    visibility: TerminalVisibility = TerminalVisibility.HIDDEN,
) -> TerminalBackend:
    """Create a PTY backend for the current platform with the requested visibility.

    ADR-0010 selection rule:

    - Windows: winpty transport serves both visibilities (two distinct subclasses
      today; structural visibility difference — see ADR-0010 Decision 4).
    - Linux/macOS with HIDDEN: pexpect (preferred), tmux (fallback).
    - Linux/macOS with VISIBLE: tmux (only).
    - The factory rejects unsupported combinations explicitly.

    Backwards-compat: on Linux/macOS the default ``visibility=HIDDEN`` is
    equivalent to the old 0-arg call (pexpect preferred, tmux fallback).
    On Windows the old 0-arg call returned ``VisibleWindowsPtyBackend``;
    the new default returns ``WindowsHiddenPtyBackend`` — **known Windows
    default-flip behaviour change**. No production caller uses the 0-arg
    factory on Windows (the Windows managers bypass the factory by
    passing ``backend_factory=WindowsHiddenPtyBackend`` /
    ``VisibleWindowsPtyBackend`` directly), but ``manager.py:TerminalManager``
    (deprecated, deleted in Phase 3 Task 9) wires
    ``self._backend_factory = create_pty_backend``, so the e2e verification
    tests ``tests/verify_terminal_e2e_*.py`` that instantiate
    ``TerminalManager(...)`` directly will silently start receiving
    ``WindowsHiddenPtyBackend`` sessions on Windows post-Phase-1. That
    migration is finalised by Phase 6 Task 13 (deferred). Until then:
    document the change, do NOT silently re-tune verify tests to fake
    VISIBLE — the folded-in ``BaseTerminalManager(visibility=...)``
    parameter is the right surface.
    """
    if sys.platform == "win32":
        if visibility == TerminalVisibility.VISIBLE:
            return _create_winpty_visible_backend()
        return _create_winpty_hidden_backend()

    # Linux / macOS
    if visibility == TerminalVisibility.VISIBLE:
        if not _is_libtmux_available():
            raise UnsupportedVisibilityForTransport(
                "No transport available for VISIBLE on this platform: "
                "libtmux is required (pexpect cannot serve VISIBLE — see ADR-0010 Decision 5)."
            )
        return _create_tmux_backend()

    # HIDDEN on Linux/macOS
    if _is_pexpect_available():
        return _create_pexpect_backend()
    if _is_libtmux_available():
        return _create_tmux_backend()
    raise UnsupportedVisibilityForTransport(
        "No transport available for HIDDEN on this platform: "
        "install pexpect (`pip install pexpect`) or libtmux (`pip install libtmux`)."
    )