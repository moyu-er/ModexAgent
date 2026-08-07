"""TerminalTool — LLM-visible tool for managing terminal sessions."""

from __future__ import annotations

import asyncio
import time
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from modex_agent.core.message import ContentFormat

from modex_agent.core.tool_manager import Tool
from modex_agent.tools.terminal.managers import TerminalManagerBase
from modex_agent.tools.terminal.process_registry import ProcessRegistry
from modex_agent.tools.terminal.prompt import resolve_cursor_line, sanitize_terminal_output
from modex_agent.utils.xml import xml_attr, xml_text


class TerminalAction(StrEnum):
    """Actions supported by TerminalTool.

    Note: a ``history`` action and the corresponding ``CommandRecord`` /
    ``TerminalSession._history`` machinery were removed because the
    implementation never actually populated the history list — CommandTool
    never appended to it, so the surface returned ``No output for terminal``
    for every tab. Use ``current`` (live snapshot) instead.
    """

    OPEN = "open"
    CLOSE = "close"
    LIST = "list"
    SELECT = "select"
    INTERRUPT = "interrupt"
    CURRENT = "current"


class TerminalTool(Tool):
    """Tool for managing named terminal sessions.

    Parameters:
        action: One of open, close, list, select, interrupt, current.
        name: Terminal tab name (optional for open/interrupt/current, required for others).
        cwd: Initial working directory (only for open).
    """

    def __init__(self, manager: TerminalManagerBase, registry: ProcessRegistry | None = None) -> None:
        super().__init__()
        self._manager = manager
        self._registry = registry

    @property
    def name(self) -> str:
        return "terminal"

    @property
    def description(self) -> str:
        return (
            "Manage persistent terminal tabs for the 'bash' and 'process' tools. "
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
                        TerminalAction.INTERRUPT,
                        TerminalAction.CURRENT,
                    ],
                    "description": "open | close | list | select | interrupt | current",
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

    def result_metadata(self, result: Any) -> tuple["ContentFormat | None", list[str] | None]:
        """Declare XML truncation metadata for <terminal_result> output."""
        from modex_agent.tools.terminal.types import terminal_result_metadata

        return terminal_result_metadata(result)

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
                f"Opened terminal tab '{target_name}'. "
                f"It is now the default — 'bash' and 'process' tools will use it."
            )

        if action_enum == TerminalAction.CLOSE:
            if not name:
                return "Error: 'name' is required for close action"
            success = await self._manager.close(name)
            return f"Closed terminal '{name}'." if success else f"Terminal '{name}' not found."

        if action_enum == TerminalAction.LIST:
            sessions = await self._manager.list_sessions()
            active = [s for s in sessions if s.is_alive]
            if not active:
                return "<terminal_result>\n<output>No active terminal tabs.</output>\n</terminal_result>"
            lines = ["<terminal_result>", "<tabs>"]
            for s in active:
                default_attr = ' default="true"' if s.is_default else ""
                proc_attr = ""
                if self._registry:
                    running = self._registry.get_running_by_terminal(s.name)
                    if running:
                        proc_attr = f' process="{xml_attr(running.command)}"'
                lines.append(
                    f'  <tab name="{xml_attr(s.name)}"{default_attr}{proc_attr} />'
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

        if action_enum == TerminalAction.INTERRUPT:
            session = await self._manager.get_default_session()
            if session is None:
                return "Error: No default terminal is active."
            await session.send_interrupt()
            # Drain output so the agent sees ^C marker and new prompt.
            await asyncio.sleep(0.3)
            await session.refresh_output(timeout=0.5)
            segment = await session.current_segment()
            cursor = sanitize_terminal_output(resolve_cursor_line(segment)).strip()
            return (
                "<terminal_result>\n"
                f"<output>\n{xml_text(cursor or '(interrupted)')}\n</output>\n"
                "</terminal_result>"
            )

        if action_enum == TerminalAction.CURRENT:
            if name:
                session = await self._manager.get_or_create(name)
            else:
                session = await self._manager.get_default_session()
            if session is None:
                return (
                    "<terminal_result>\n"
                    "<status>unknown</status>\n"
                    "<output>No terminal is active. Use terminal open to create one.</output>\n"
                    "</terminal_result>"
                )

            status = await session.command_status()
            output = await session.last_command_output()

            raw_idle_ms = int((time.monotonic() - session.last_byte_at) * 1000)
            no_output_ms_str = str(raw_idle_ms) if raw_idle_ms > 0 else None

            parts = [
                "<terminal_result>",
                f"<status>{status.value}</status>",
            ]
            if no_output_ms_str:
                parts.append(f"<no_output_ms>{no_output_ms_str}</no_output_ms>")

            parts.append(f"<tab_name>{xml_text(session.name)}</tab_name>")

            if self._registry:
                running = self._registry.get_running_by_terminal(session.name)
                if running:
                    parts.append(f"<running_command>{xml_text(running.command)}</running_command>")

            parts.append(f"<output>\n{xml_text(sanitize_terminal_output(output) or '(no output yet)')}\n</output>")

            # Interference detection for visible terminals
            if session.detect_interference(status):
                expected = session._expected_state
                assert expected is not None
                parts.append(
                    "<interference>"
                    f"<expected>{expected.value}</expected>"
                    f"<actual>{status.value}</actual>"
                    "<message>"
                    "Terminal state changed unexpectedly — user may have interacted with the visible window. "
                    "Verify the current screen content above before proceeding."
                    "</message>"
                    "</interference>"
                )

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
