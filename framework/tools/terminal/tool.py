"""TerminalTool — LLM-visible tool for managing terminal sessions."""

from __future__ import annotations

import time
from enum import StrEnum
from typing import Any
from xml.sax.saxutils import escape as xml_escape

from framework.core.tool_manager import Tool
from framework.tools.terminal.managers import TerminalManagerBase
from framework.tools.terminal.process_registry import ProcessRegistry
from framework.tools.terminal.prompt import resolve_cursor_line


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

    def __init__(self, manager: TerminalManagerBase, registry: ProcessRegistry | None = None):
        super().__init__()
        self._manager = manager
        self._registry = registry

    @property
    def name(self) -> str:
        return "terminal"

    @property
    def description(self) -> str:
        return (
            "Manage persistent terminal tabs for the 'command' and 'process' tools. "
            "Every command runs in the CURRENTLY SELECTED tab — use 'open' or 'select' "
            "to switch context before running commands.\n\n"
            "Actions:\n"
            "  open      — create a new tab AND auto-select it (the next command runs there)\n"
            "  close     — close a tab by name; cannot close the default tab if it's the last one\n"
            "  list      — list all tabs; the '(default)' marker shows which tab commands target\n"
            "  select    — switch the default tab; all subsequent commands run in this tab\n"
            "  current   — see the terminal screen (last 30 lines) + status of the default tab\n"
            "  interrupt — send Ctrl+C to stop a running command in the default tab\n\n"
            "Workflow: to work on a separate task, open a new tab (it auto-selects), run "
            "commands there, then select back to the previous tab when done. Tabs are "
            "independent: each has its own shell session, working directory, and environment."
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
            return (
                f"Opened terminal '{target_name}' ({session.shell_info.name}) "
                f"at timestamp {int(session.created_at)}. "
                f"This tab is now the default — 'command' and 'process' tools will use it."
            )

        if action_enum == TerminalAction.CLOSE:
            if not name:
                return "Error: 'name' is required for close action"
            success = await self._manager.close(name)
            return f"Closed terminal '{name}'." if success else f"Terminal '{name}' not found."

        if action_enum == TerminalAction.LIST:
            sessions = await self._manager.list_sessions()
            if not sessions:
                return "<terminal_result>\n<action>list</action>\n<output>No active terminals.</output>\n</terminal_result>"
            lines = ["<terminal_result>", "<action>list</action>", "<tabs>"]
            for s in sessions:
                default_attr = ' default="true"' if s.is_default else ""
                alive_attr = ' alive="false"' if not s.is_alive else ""
                proc_attr = ""
                if self._registry:
                    running = self._registry.get_running_by_terminal(s.name)
                    if running:
                        proc_attr = f' process="{xml_escape(running.command)}"'
                lines.append(
                    f'  <tab name="{xml_escape(s.name)}" shell="{s.shell_type}" '
                    f'created_at="{int(s.created_at)}" commands="{s.command_count}"{default_attr}{alive_attr}{proc_attr} />'
                )
            lines.append("</tabs>")
            lines.append("</terminal_result>")
            return "\n".join(lines)

        if action_enum == TerminalAction.SELECT:
            if not name:
                return "Error: 'name' is required for select action"
            try:
                await self._manager.select_default(name)
                return f"Selected '{name}' as default terminal. All 'command' and 'process' tool calls now target this tab."
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
                    "<status>unknown</status>\n"
                    "<output>No terminal is active. Use terminal open to create one.</output>\n"
                    "</terminal_result>"
                )

            status = await session.command_status()
            output = await session.last_command_output()
            segment = await session.current_segment()
            cursor = resolve_cursor_line(segment).strip()

            raw_idle_ms = int((time.monotonic() - session._last_byte_at) * 1000)
            idle_ms_str = str(raw_idle_ms) if raw_idle_ms > 0 else None

            default_session = await self._manager.get_default_session()
            is_default = default_session is not None and session.name == default_session.name

            parts = [
                "<terminal_result>",
                "<action>current</action>",
                f"<terminal>{xml_escape(session.name)}</terminal>",
                f"<created_at>{int(session.created_at)}</created_at>",
                f"<default>{str(is_default).lower()}</default>",
                f"<status>{status.value}</status>",
            ]
            if cursor:
                parts.append(f"<cursor>{xml_escape(cursor)}</cursor>")
            if idle_ms_str:
                parts.append(f"<idle_ms>{idle_ms_str}</idle_ms>")
            parts.append(f"<output>{xml_escape(output or '(no output yet)')}</output>")
            parts.append("</terminal_result>")
            return "\n".join(parts)

        return f"Error: Unhandled action '{action}'"

    def _auto_name(self) -> str:
        """Generate auto-incremented tab name."""
        existing = set(self._manager.list_names())
        for i in range(1, 1000):
            name = f"tab-{i}"
            if name not in existing:
                return name
        return "tab-auto"
