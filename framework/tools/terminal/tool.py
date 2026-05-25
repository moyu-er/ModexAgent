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
    INTERRUPT = "interrupt"
    CURRENT = "current"


class TerminalTool(Tool):
    """Tool for managing named terminal sessions.

    Parameters:
        action: One of open, close, list, select, history, interrupt.
        name: Terminal name (optional for open/interrupt, required for others).
        cwd: Initial working directory (only for open).
    """

    def __init__(self, manager: TerminalManager):
        super().__init__()
        self._manager = manager

    @property
    def name(self) -> str:
        return "terminal"

    @property
    def description(self) -> str:
        return (
            "Manage terminal tabs/sessions. "
            "Actions: open (create a new tab), close (terminate a tab), list (show all tabs), "
            "select (switch default tab), history (show recent output of a tab), "
            "interrupt (send Ctrl+C to the current default tab). "
            "IMPORTANT: This tool does NOT execute commands — use the shell tool for that. "
            "You generally do NOT need to open a terminal before using the shell tool."
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
                        TerminalAction.INTERRUPT,
                        TerminalAction.CURRENT,
                    ],
                    "description": "Action to perform",
                },
                "name": {
                    "type": "string",
                    "description": "Terminal name (optional for open/interrupt, required for close/select/history)",
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
            await session.ensure_started()
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
                await self._manager.select_default(name)
                return f"Selected '{name}' as default terminal."
            except ValueError as e:
                return f"Error: {e}"

        if action_enum == TerminalAction.HISTORY:
            if not name:
                return "Error: 'name' is required for history action"
            session = self._manager.get(name)
            if session is None:
                return f"Error: Terminal '{name}' not found."
            records = session.get_history()
            if not records:
                return f"No output for terminal '{name}'."
            last = records[-1]
            lines = last.output.splitlines()
            recent = "\n".join(lines[-20:])
            return f"Recent output for '{name}':\n{recent}"

        if action_enum == TerminalAction.INTERRUPT:
            session = await self._manager.get_default_session()
            if session is None:
                return "Error: No default terminal is active."
            await session.send_interrupt()
            return f"Sent Ctrl+C to terminal '{session.name}'."

        if action_enum == TerminalAction.CURRENT:
            if name:
                session = await self._manager.get_or_create(name)
            else:
                session = await self._manager.get_default_session()
            if session is None:
                return "Error: No terminal is active."
            segment = await session.current_segment()
            return (
                f"Current terminal segment:\n"
                f"{segment.text}\n"
                f"empty_prompt={segment.is_empty_prompt}"
            )

        return f"Error: Unhandled action '{action}'"

    def _auto_name(self) -> str:
        """Generate auto-incremented tab name."""
        existing = set(self._manager.list_names())
        for i in range(1, 1000):
            name = f"tab-{i}"
            if name not in existing:
                return name
        return "tab-auto"
