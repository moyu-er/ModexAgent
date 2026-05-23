"""TerminalTool — LLM-visible tool for managing terminal sessions."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from framework.core.tool_manager import Tool
from framework.tools.terminal.manager import TerminalManager


class TerminalAction(StrEnum):
    """Actions supported by TerminalTool."""

    OPEN = "open"
    CLOSE = "close"
    LIST = "list"
    SELECT = "select"
    HISTORY = "history"


class TerminalTool(Tool):
    """Tool for managing named terminal sessions.

    Parameters:
        action: One of open, close, list, select, history.
        name: Terminal name (optional for open, required otherwise).
        cwd: Initial working directory (only for open).
    """

    def __init__(self, manager: TerminalManager):
        super().__init__()
        self._manager = manager

    @property
    def name(self) -> str:
        return "terminal_manager"

    @property
    def description(self) -> str:
        return (
            "Manage persistent terminal sessions. "
            "Actions: open (create), close (terminate), list (show all), "
            "select (set default), history (show recent commands). "
            "After opening/selecting a terminal, use the shell tool to execute commands in it."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        TerminalAction.OPEN,
                        TerminalAction.CLOSE,
                        TerminalAction.LIST,
                        TerminalAction.SELECT,
                        TerminalAction.HISTORY,
                    ],
                    "description": "Action to perform",
                },
                "name": {
                    "type": "string",
                    "description": "Terminal name (optional for open, required for others)",
                },
                "cwd": {
                    "type": "string",
                    "description": "Initial working directory (only for open)",
                },
            },
            "required": ["action"],
        }

    async def execute(
        self,
        action: str,
        name: str | None = None,
        cwd: str | None = None,
        **kwargs: Any,
    ) -> str:
        try:
            action_enum = TerminalAction(action)
        except ValueError:
            valid = ", ".join(TerminalAction)
            return f"Error: Unknown action '{action}'. Valid actions: {valid}"

        if action_enum == TerminalAction.OPEN:
            target_name = name or self._auto_name()
            session = await self._manager.get_or_create(target_name, cwd=cwd)
            return f"Opened terminal '{target_name}' ({session.shell_info.name})."

        if action_enum == TerminalAction.CLOSE:
            if not name:
                return "Error: 'name' is required for close action"
            success = await self._manager.close(name)
            return f"Closed terminal '{name}'." if success else f"Terminal '{name}' not found."

        if action_enum == TerminalAction.LIST:
            sessions = await self._manager.list_sessions()
            if not sessions:
                return "No active terminals."
            lines = ["Active terminals:"]
            for s in sessions:
                default_marker = " (default)" if s.is_default else ""
                alive_marker = "" if s.is_alive else " [dead]"
                lines.append(
                    f"  - {s.name}: {s.shell_type}, "
                    f"commands={s.command_count}{default_marker}{alive_marker}"
                )
            return "\n".join(lines)

        if action_enum == TerminalAction.SELECT:
            if not name:
                return "Error: 'name' is required for select action"
            try:
                self._manager.select_default(name)
                return f"Selected '{name}' as default terminal."
            except ValueError as e:
                return f"Error: {e}"

        if action_enum == TerminalAction.HISTORY:
            if not name:
                return "Error: 'name' is required for history action"
            history = self._manager.get_history(name)
            if not history:
                return f"No history for terminal '{name}'."
            lines = [f"History for '{name}':"]
            for rec in history:
                lines.append(f"  > {rec.command}")
                if rec.output:
                    lines.append(f"    {rec.output[:80]}")
            return "\n".join(lines)

        return f"Error: Unhandled action '{action}'"

    def _auto_name(self) -> str:
        """Generate auto-incremented tab name."""
        existing = set(self._manager.list_names())
        for i in range(1, 1000):
            name = f"tab-{i}"
            if name not in existing:
                return name
        return "tab-auto"
