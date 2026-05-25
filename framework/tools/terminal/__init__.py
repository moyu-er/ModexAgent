"""Terminal management tools and backends."""

from __future__ import annotations

from framework.tools.terminal.command_tool import CommandTool
from framework.tools.terminal.manager import TerminalManager
from framework.tools.terminal.managers import (
    BaseTerminalManager,
    TerminalManagerBase,
    WindowsHiddenTerminalManager,
    WindowsVisibleTerminalManager,
    create_terminal_manager,
)
from framework.tools.terminal.process_registry import ProcessRegistry
from framework.tools.terminal.process_tool import ProcessTool
from framework.tools.terminal.session import CommandRecord, TerminalInfo, TerminalSession
from framework.tools.terminal.state_store import JsonTerminalStateStore
from framework.tools.terminal.subprocess_tool import ShellExecutor, SubprocessExecutor, SubprocessTool
from framework.tools.terminal.tool import TerminalAction, TerminalTool

__all__ = [
    "BaseTerminalManager",
    "CommandRecord",
    "CommandTool",
    "JsonTerminalStateStore",
    "ProcessRegistry",
    "ProcessTool",
    "ShellExecutor",
    "SubprocessExecutor",
    "SubprocessTool",
    "TerminalAction",
    "TerminalInfo",
    "TerminalManager",
    "TerminalManagerBase",
    "TerminalSession",
    "TerminalTool",
    "WindowsHiddenTerminalManager",
    "WindowsVisibleTerminalManager",
    "create_terminal_manager",
]
