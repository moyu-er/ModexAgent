"""Terminal management tools and backends."""

from __future__ import annotations

from modex_agent.tools.terminal.command_tool import CommandTool
from modex_agent.tools.terminal.manager import TerminalManager
from modex_agent.tools.terminal.managers import (
    BaseTerminalManager,
    TerminalManagerBase,
    create_terminal_manager,
    create_terminal_manager_or_none,
)
from modex_agent.tools.terminal.process_registry import ProcessRegistry
from modex_agent.tools.terminal.process_tool import ProcessTool
from modex_agent.tools.terminal.session import TerminalInfo, TerminalSession
from modex_agent.tools.terminal.subprocess_tool import (
    CmdSubprocessExecutor,
    PosixSubprocessExecutor,
    ShellExecutor,
    SubprocessExecutor,
    SubprocessTool,
    create_subprocess_executor,
)
from modex_agent.tools.terminal.tool import TerminalAction, TerminalTool

__all__ = [
    "BaseTerminalManager",
    "CmdSubprocessExecutor",
    "CommandTool",
    "PosixSubprocessExecutor",
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
    "create_subprocess_executor",
    "create_terminal_manager",
    "create_terminal_manager_or_none",
]
