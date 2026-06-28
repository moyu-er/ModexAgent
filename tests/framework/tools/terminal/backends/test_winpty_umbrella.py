"""Winpty umbrella base — both Windows backends share the transport-level name."""

from __future__ import annotations

import sys

import pytest

if not sys.platform.startswith("win"):
    pytest.skip("Windows-only backend layer", allow_module_level=True)

from modex_agent.tools.terminal.backends.visible_windows import VisibleWindowsPtyBackend
from modex_agent.tools.terminal.backends.windows_hidden import WindowsHiddenPtyBackend
from modex_agent.tools.terminal.backends.winpty import WinptyBackend


def test_visible_windows_backend_is_winpty_transport() -> None:
    assert issubclass(VisibleWindowsPtyBackend, WinptyBackend)


def test_windows_hidden_backend_is_winpty_transport() -> None:
    assert issubclass(WindowsHiddenPtyBackend, WinptyBackend)
