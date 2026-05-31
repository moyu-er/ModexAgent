"""TerminalTool — LLM-visible tool for managing terminal sessions."""

from __future__ import annotations

from enum import StrEnum
from typing import Any
from xml.sax.saxutils import escape as xml_escape

from framework.core.tool_manager import Tool
from framework.tools.terminal.managers import TerminalManagerBase
from framework.tools.terminal.prompt import (
    detect_pager_entry,
    resolve_cursor_line,
    sanitize_terminal_output,
)


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

    def __init__(self, manager: TerminalManagerBase):
        super().__init__()
        self._manager = manager

    @property
    def name(self) -> str:
        return "terminal"

    @property
    def description(self) -> str:
        return (
            "Manage persistent terminal tabs. Actions:\n"
            "  open     -- create a named tab (optional: cwd for initial directory)\n"
            "  close    -- terminate a tab\n"
            "  list     -- show all tabs with status\n"
            "  select   -- switch which tab 'command' and 'process' tools target\n"
            "  current  -- show what is visible in the active tab\n"
            "  interrupt-- send Ctrl+C to the current tab\n\n"
            "Each tab has its own independent shell session (separate cd, env, etc.). "
            "The default tab is created automatically; you only need 'open' to create "
            "additional named tabs for parallel work. Use 'select' to switch between them."
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
                    "description": "open | close | list | select | history | interrupt | current",
                },
                "name": {
                    "type": "string",
                    "description": "Tab name. Required for close/select. Optional for open (default name if omitted).",
                },
                "cwd": {
                    "type": "string",
                    "description": "Initial working directory for new tab (open action only)",
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
                return (
                    "<terminal_result>\n"
                    "<action>current</action>\n"
                    "<status>none</status>\n"
                    "<output>No terminal is active. Use terminal open to create one.</output>\n"
                    "</terminal_result>"
                )
            segment = await session.current_segment()
            cleaned = sanitize_terminal_output(segment.text).rstrip()

            if segment.is_empty_prompt:
                status = "idle"
            elif session.busy_after_timeout:
                status = "busy"
            elif session.last_status == "waiting_input":
                status = "waiting_input"
            elif detect_pager_entry(resolve_cursor_line(segment)):
                status = "pager"
            else:
                status = "active"

            cursor = segment.cursor_line.strip() if segment.cursor_line else ""
            output_lines = cleaned.splitlines()[-30:] if cleaned else []
            output_text = "\n".join(output_lines) if output_lines else "(terminal is idle — no output yet)"

            return (
                "<terminal_result>\n"
                "<action>current</action>\n"
                f"<terminal>{xml_escape(session.name)}</terminal>\n"
                f"<status>{status}</status>\n"
                f"<cursor>{xml_escape(cursor)}</cursor>\n"
                f"<output>{xml_escape(output_text)}</output>\n"
                "</terminal_result>"
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
