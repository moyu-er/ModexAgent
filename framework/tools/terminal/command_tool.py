"""CommandTool — execute commands in terminal sessions.

Three-tier completion detection:
  1. Process exit (authoritative)
  2. Prompt detection (auxiliary completion for persistent shells)
  3. Timeout (kills process, returns partial output)

Returns structured <command_result> XML with CommandResultStatus.
"""

from __future__ import annotations

import time
from xml.sax.saxutils import escape as xml_escape

from framework.core.tool_manager import Tool
from framework.tools.terminal.config import TerminalRuntimeConfig
from framework.tools.terminal.managers import TerminalManagerBase
from framework.tools.terminal.process_registry import ProcessRegistry, RunningSessionRuntime
from framework.tools.terminal.prompt import (
    resolve_cursor_line,
    sanitize_terminal_output,
)
from framework.tools.terminal.pty_keys import CursorKeyMode
from framework.tools.terminal.session import TerminalSession
from framework.tools.terminal.types import CommandResultStatus, ProcessStatus


def _build_command_xml(
    output: str,
    status: CommandResultStatus,
    elapsed_ms: int,
    *,
    terminal: str | None = None,
    idle_ms: int | None = None,
    pages_scrolled: int | None = None,
    truncated: bool | None = None,
    message: str | None = None,
) -> str:
    """Build a <command_result> XML string."""
    parts: list[str] = [
        "<command_result>",
    ]
    if terminal is not None:
        parts.append(f"<terminal>{xml_escape(terminal)}</terminal>")
    parts.extend([
        f"<output>{xml_escape(output)}</output>",
        f"<status>{status.value}</status>",
        f"<elapsed_ms>{elapsed_ms}</elapsed_ms>",
    ])
    if idle_ms is not None:
        parts.append(f"<idle_ms>{idle_ms}</idle_ms>")
    if pages_scrolled is not None:
        parts.append(f"<pages_scrolled>{pages_scrolled}</pages_scrolled>")
    if truncated is not None:
        parts.append(f"<truncated>{str(truncated).lower()}</truncated>")
    if message is not None:
        parts.append(f"<message>{xml_escape(message)}</message>")
    parts.append("</command_result>")
    return "\n".join(parts)


class CommandTool(Tool):
    """Execute a command in the default terminal session."""

    def __init__(
        self,
        manager: TerminalManagerBase,
        registry: ProcessRegistry,
        config: TerminalRuntimeConfig | None = None,
    ) -> None:
        super().__init__()
        self._manager = manager
        self._registry = registry
        self._config = config or TerminalRuntimeConfig()
        self.config.timeout = self._config.command_tool_outer_timeout_seconds

    @property
    def name(self) -> str:
        return "bash"

    @property
    def description(self) -> str:
        return (
            "Execute a shell command in a persistent terminal session. "
            "Working directory, environment variables, and background "
            "processes persist between calls in the same session.\n\n"
            "Use 'terminal list' to see all sessions and which is selected (default). "
            "Use 'terminal select <name>' to switch sessions; use 'terminal open <name>' "
            "to create a new session (it auto-selects).\n\n"
            "Do NOT re-run setup commands (cd, source, export, etc.) that were "
            "already executed in this session.\n\n"
            "Returns <command_result> XML with <status>: completed, executing, "
            "timed_out, paginated, waiting_input, or stuck. If <status> is not 'completed', "
            "use 'terminal current' to check the state.\n\n"
            "IMPORTANT: If a command asks for a password, STOP and ask the user. "
            "NEVER guess or invent passwords."
        )

    @property
    def parameters(self) -> dict[str, object]:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to execute in the persistent terminal session",
                },
            },
            "required": ["command"],
        }

    async def execute(
        self,
        command: str,
        **_kwargs: object,
    ) -> str:
        session = await self._manager.get_default()
        terminal_name = session.name
        await session.ensure_started()

        proc = self._registry.create(
            command=command,
            terminal=terminal_name,
            cwd=None,
            pid=None,
        )

        await session.submit_command(command)

        from framework.tools.terminal.poll_loop import PollOutcome, poll_until_settled

        timeout_seconds = self._config.default_command_timeout_seconds
        yield_window_ms = self._config.default_yield_ms

        result = await poll_until_settled(
            session, self._registry, proc.id, self._config,
            yield_ms=yield_window_ms,
            timeout_seconds=timeout_seconds,
            check_input_wait=True,
        )

        match result.outcome:
            case PollOutcome.PROCESS_EXIT:
                self._registry.mark_exited(
                    proc.id, exit_code=None, exit_signal=None,
                    status=ProcessStatus.COMPLETED,
                )
                return self._format_completed(result.output_parts, result.elapsed_ms, terminal=terminal_name)
            case PollOutcome.PROMPT_DETECTED:
                self._registry.mark_exited(
                    proc.id, exit_code=None, exit_signal=None,
                    status=ProcessStatus.COMPLETED,
                )
                return self._format_completed(result.output_parts, result.elapsed_ms, terminal=terminal_name)
            case PollOutcome.INPUT_WAIT:
                runtime = self._registry.running_runtime(proc.id)
                return await self._format_running(
                    session, result.output_parts, runtime, result.elapsed_ms,
                    detected_input_wait=True, terminal=terminal_name,
                )
            case PollOutcome.STUCK:
                raw_idle_ms = int((time.monotonic() - session.last_byte_at) * 1000)
                return self._format_stuck(result.output_parts, raw_idle_ms, result.elapsed_ms, terminal=terminal_name)
            case PollOutcome.YIELDED:
                return await self._format_running(
                    session, result.output_parts, None, result.elapsed_ms,
                    terminal=terminal_name,
                )
            case PollOutcome.TIMED_OUT:
                await session.terminate()
                self._registry.mark_exited(
                    proc.id, exit_code=None, exit_signal="TIMEOUT",
                    status=ProcessStatus.TIMED_OUT, timed_out=True,
                )
                return self._format_timed_out(result.output_parts, timeout_seconds, result.elapsed_ms, terminal=terminal_name)

    # ------------------------------------------------------------------
    # XML formatting
    # ------------------------------------------------------------------

    @staticmethod
    def _format_completed(output_parts: list[str], elapsed_ms: int, *, terminal: str | None = None) -> str:
        raw = "".join(output_parts)
        output = sanitize_terminal_output(raw).rstrip()
        return _build_command_xml(
            output or "(no output)",
            CommandResultStatus.COMPLETED,
            elapsed_ms,
            terminal=terminal,
        )

    @staticmethod
    async def _format_running(
        terminal_session: TerminalSession,
        output_parts: list[str],
        runtime: RunningSessionRuntime | None,
        elapsed_ms: int,
        *,
        detected_input_wait: bool = False,
        terminal: str | None = None,
    ) -> str:
        raw = "".join(output_parts)
        output = sanitize_terminal_output(raw).rstrip()
        idle_ms = runtime.idle_ms if runtime else None

        is_input_wait = detected_input_wait or (runtime is not None and runtime.waiting_for_input)
        if is_input_wait:
            message = (
                f"No new output for {(runtime.idle_ms if runtime else 0) // 1000}s; "
                "this session may be waiting for input. "
                "Use process write, send_keys, submit, or paste to provide input."
            )
            return _build_command_xml(
                output, CommandResultStatus.WAITING_INPUT, elapsed_ms,
                terminal=terminal, idle_ms=idle_ms, message=message,
            )

        message = (
            "Command still executing. Use terminal current to check progress, "
            "process write/send_keys/paste for input."
        )
        xml = _build_command_xml(
            output, CommandResultStatus.EXECUTING, elapsed_ms,
            terminal=terminal, idle_ms=idle_ms, message=message,
        )

        if terminal_session.cursor_key_mode == CursorKeyMode.APPLICATION:
            segment = await terminal_session.current_segment()
            if segment and segment.text.strip():
                tui_text = sanitize_terminal_output(segment.text).rstrip()
                xml = xml.replace(
                    "</command_result>",
                    f"\n<tui_screen>{xml_escape(tui_text)}</tui_screen>\n</command_result>",
                )
        else:
            segment = await terminal_session.current_segment()
            cursor = resolve_cursor_line(segment)
            if cursor.strip():
                cursor_text = sanitize_terminal_output(cursor).rstrip()
                xml = xml.replace(
                    "</command_result>",
                    f"\n<cursor_line>{xml_escape(cursor_text)}</cursor_line>\n</command_result>",
                )

        return xml

    @staticmethod
    def _format_stuck(
        output_parts: list[str],
        raw_idle_ms: int,
        elapsed_ms: int,
        *,
        terminal: str | None = None,
    ) -> str:
        raw = "".join(output_parts)
        output = sanitize_terminal_output(raw).rstrip()
        message = (
            f"No terminal activity for {raw_idle_ms // 1000}s. "
            "The command may be stuck. Use process interrupt to send Ctrl+C, "
            "or terminal current to check the screen."
        )
        return _build_command_xml(
            output, CommandResultStatus.STUCK, elapsed_ms,
            terminal=terminal, idle_ms=raw_idle_ms, message=message,
        )

    @staticmethod
    def _format_timed_out(
        output_parts: list[str],
        timeout_seconds: int,
        elapsed_ms: int,
        *,
        terminal: str | None = None,
    ) -> str:
        raw = "".join(output_parts)
        output = sanitize_terminal_output(raw).rstrip()
        message = (
            f"Command timed out after {timeout_seconds}s and was terminated. "
            "Partial output captured above."
        )
        return _build_command_xml(
            output, CommandResultStatus.TIMED_OUT, elapsed_ms,
            terminal=terminal, message=message,
        )

