"""TmuxBackend — visibility is a switch parameter (ADR-0010 Decision 4)."""

from __future__ import annotations

import sys

import pytest


def _libtmux_available() -> bool:
    try:
        import libtmux  # noqa: F401

        return True
    except ImportError:
        return False


if sys.platform.startswith("win") or not _libtmux_available():
    pytest.skip("tmux backend requires Unix + libtmux", allow_module_level=True)


from modex_agent.tools.terminal.backends.tmux_pty import TmuxPtyBackend  # noqa: E402
from modex_agent.tools.terminal.types import TerminalVisibility  # noqa: E402


def test_default_visibility_is_hidden() -> None:
    backend = TmuxPtyBackend()
    assert backend.visibility == TerminalVisibility.HIDDEN


def test_construct_with_visible_attribute_exposed() -> None:
    backend = TmuxPtyBackend(visibility=TerminalVisibility.VISIBLE)
    assert backend.visibility == TerminalVisibility.VISIBLE


def test_hidden_attaches_false_flag_carried() -> None:
    backend = TmuxPtyBackend()
    # internal flag mirrors visibility — tested via a property to avoid coupling to attribute name
    assert backend._attach is False


def test_visible_attaches_true_flag_carried() -> None:
    backend = TmuxPtyBackend(visibility=TerminalVisibility.VISIBLE)
    assert backend._attach is True
