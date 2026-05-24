"""Terminal management tools and backends."""

from __future__ import annotations

from framework.tools.terminal.manager import TerminalManager
from framework.tools.terminal.session import CommandRecord, TerminalInfo, TerminalSession
from framework.tools.terminal.state_store import JsonTerminalStateStore
from framework.tools.terminal.tool import TerminalAction, TerminalTool

__all__ = [
    "CommandRecord",
    "JsonTerminalStateStore",
    "TerminalAction",
    "TerminalInfo",
    "TerminalManager",
    "TerminalSession",
    "TerminalTool",
]
