"""Terminal management tools and backends."""

from __future__ import annotations

from modex_agent.tools.terminal.command_tool import CommandTool
from modex_agent.tools.terminal.manager import TerminalManager
from modex_agent.tools.terminal.managers import (
    BaseTerminalManager,
    TerminalManagerBase,
    create_terminal_manager,
)
from modex_agent.tools.terminal.process_registry import ProcessRegistry
from modex_agent.tools.terminal.process_tool import ProcessTool
from modex_agent.tools.terminal.session import CommandRecord, TerminalInfo, TerminalSession
from modex_agent.tools.terminal.state_store import JsonTerminalStateStore
from modex_agent.tools.terminal.subprocess_tool import (
    ShellExecutor,
    SubprocessExecutor,
    SubprocessTool,
)
from modex_agent.tools.terminal.tool import TerminalAction, TerminalTool

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
    "create_terminal_manager",
]
