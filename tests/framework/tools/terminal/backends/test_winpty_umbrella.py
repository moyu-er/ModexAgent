"""Winpty umbrella base — both Windows backends share the transport-level name."""

from __future__ import annotations

import sys

import pytest

if not sys.platform.startswith("win"):
    pytest.skip("Windows-only backend layer", allow_module_level=True)

from modex_agent.tools.terminal.backends.visible_windows import WinptyConsoleWindowBackend
from modex_agent.tools.terminal.backends.windows_hidden import WinptyHiddenBackend
from modex_agent.tools.terminal.backends.winpty_transport import WinptyBackend


def test_winpty_console_window_is_winpty_transport() -> None:
    assert issubclass(WinptyConsoleWindowBackend, WinptyBackend)
    assert WinptyConsoleWindowBackend.visibility.value == "visible"


def test_winpty_hidden_is_winpty_transport() -> None:
    assert issubclass(WinptyHiddenBackend, WinptyBackend)
    assert WinptyHiddenBackend.visibility.value == "hidden"
