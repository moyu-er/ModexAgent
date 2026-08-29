"""TerminalTool — LLM-visible tool for managing terminal sessions."""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from modex_agent.core.message import ContentFormat

from modex_agent.core.tool_manager import Tool
from modex_agent.tools.terminal.managers import TerminalManagerBase
from modex_agent.tools.terminal.process_registry import ProcessRegistry
from modex_agent.utils.xml import xml_attr


class TerminalAction(StrEnum):
    """Actions supported by TerminalTool."""

    OPEN = "open"
    CLOSE = "close"
    LIST = "list"
    SELECT = "select"


class TerminalTool(Tool):
    """Tool for managing named terminal sessions.

    Parameters:
        action: One of open, close, list, select.
        name: Terminal tab name (optional for open, required for close/select).
    """

    def __init__(
        self, manager: TerminalManagerBase, registry: ProcessRegistry | None = None
    ) -> None:
        super().__init__()
        self._manager = manager
        self._registry = registry

    @property
    def name(self) -> str:
        return "terminal"

    @property
    def description(self) -> str:
        return """Manage terminal tabs for the bash and process tools. Commands always run in the
currently SELECTED tab.

- open — create a new tab AND select it (starts in the workspace directory)
- close — close a tab by name (its shell dies)
- list — show tabs; '(default)' marks the selected one, with its running command
- select — switch the selected tab

Tabs are independent (own shell, cwd, environment). You rarely need more than one."""

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
                    ],
                    "description": "open | close | list | select",
                },
                "name": {
                    "type": "string",
                    "description": "Tab name. Required for close/select. Optional for open (default name if omitted).",
                },
            },
            "required": ["action"],
        }

    def result_metadata(self, result: Any) -> tuple[ContentFormat | None, list[str] | None]:
        """Declare XML truncation metadata for <terminal_result> output."""
        from modex_agent.tools.terminal.types import terminal_result_metadata

        return terminal_result_metadata(result)

    async def execute(
        self,
        action: str = "",
        name: str | None = None,
        **kwargs: Any,
    ) -> str:
        try:
            action_enum = TerminalAction(action)
        except ValueError:
            valid = ", ".join(TerminalAction)
            return f"Error: Unknown action '{action}'. Valid actions: {valid}"

        if action_enum == TerminalAction.OPEN:
            target_name = name or self._auto_name()
            session = await self._manager.get_or_create(target_name)
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
                lines.append(f'  <tab name="{xml_attr(s.name)}"{default_attr}{proc_attr} />')
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

        return f"Error: Unhandled action '{action}'"

    def _auto_name(self) -> str:
        """Generate auto-incremented tab name."""
        existing = set(self._manager.list_names())
        for i in range(1, 1000):
            name = f"tab-{i}"
            if name not in existing:
                return name
        return "tab-auto"
