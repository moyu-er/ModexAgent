"""Terminal backend implementations."""

from __future__ import annotations

from modex_agent.tools.terminal.backends.visible_windows import WinptyConsoleWindowBackend
from modex_agent.tools.terminal.backends.windows_hidden import WinptyHiddenBackend

# Deprecated aliases — kept for one-to-two release migration window.
VisibleWindowsPtyBackend = WinptyConsoleWindowBackend
WindowsHiddenPtyBackend = WinptyHiddenBackend

__all__: list[str] = [
    "WinptyConsoleWindowBackend",
    "WinptyHiddenBackend",
    "VisibleWindowsPtyBackend",  # deprecated alias
    "WindowsHiddenPtyBackend",  # deprecated alias
]
