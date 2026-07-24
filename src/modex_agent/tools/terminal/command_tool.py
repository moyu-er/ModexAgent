"""CommandTool — execute commands in terminal sessions.

Three-tier completion detection:
  1. Process exit (authoritative)
  2. Prompt detection (auxiliary completion for persistent shells)
  3. Timeout (kills process, returns partial output)

Returns structured <command_result> XML with CommandResultStatus.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from modex_agent.core.message import ContentFormat

from modex_agent.core.tool_manager import Tool
from modex_agent.tools.terminal.config import TerminalRuntimeConfig
from modex_agent.tools.terminal.guard import check_command_writable
from modex_agent.tools.terminal.managers import TerminalManagerBase
from modex_agent.tools.terminal.process_registry import ProcessRegistry, RunningSessionRuntime
from modex_agent.tools.terminal.prompt import (
    resolve_cursor_line,
    sanitize_terminal_output,
)
from modex_agent.tools.terminal.pty_keys import CursorKeyMode
from modex_agent.tools.terminal.session import TerminalSession
from modex_agent.tools.terminal.types import CommandResultStatus, ProcessStatus, TerminalCommandStatus
from modex_agent.utils.xml import xml_text


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
    hint: str | None = None,
) -> str:
    """Build a <command_result> XML string."""
    parts: list[str] = [
        "<command_result>",
    ]
    if terminal is not None:
        parts.append(f"<terminal>{xml_text(terminal)}</terminal>")
    if hint is not None:
        parts.append(f"<hint>{xml_text(hint)}</hint>")
    parts.extend(
        [
            f"<output>{xml_text(output)}</output>",
            f"<status>{status.value}</status>",
            f"<elapsed_ms>{elapsed_ms}</elapsed_ms>",
        ]
    )
    if idle_ms is not None:
        parts.append(f"<idle_ms>{idle_ms}</idle_ms>")
    if pages_scrolled is not None:
        parts.append(f"<pages_scrolled>{pages_scrolled}</pages_scrolled>")
    if truncated is not None:
        parts.append(f"<truncated>{str(truncated).lower()}</truncated>")
    if message is not None:
        parts.append(f"<message>{xml_text(message)}</message>")
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

    def result_metadata(self, result: Any) -> tuple["ContentFormat | None", list[str] | None]:
        """Declare XML truncation metadata for <command_result> output."""
        from modex_agent.tools.terminal.types import terminal_result_metadata

        return terminal_result_metadata(result)

    async def execute(
        self,
        command: str,
        **_kwargs: object,
    ) -> str:
        from modex_agent.runtime.env_context import _current_session_id, _modex_env
        from modex_agent.tools.terminal.env import build_full_env

        sid = _current_session_id.get()
        overrides = _modex_env.get()
        if sid is not None:
            session = await self._manager.get_or_create(sid)
        else:
            session = await self._manager.get_default()
        terminal_name = session.name
        is_new_tab = not session.backend_started

        # Guard: check terminal is writable before proceeding
        guard_result = await check_command_writable(session, config=self._config)
        if guard_result is not None:
            return self._format_rejected(guard_result, terminal=terminal_name)

        session.set_expected_state(TerminalCommandStatus.EXECUTING)
        await session.ensure_started(env=build_full_env(overrides) if overrides else None)

        proc = self._registry.create(
            command=command,
            terminal=terminal_name,
            cwd=None,
            pid=None,
        )

        await session.submit_command(command)

        from modex_agent.tools.terminal.poll_loop import PollOutcome, poll_until_settled

        timeout_seconds = self._config.default_command_timeout_seconds
        yield_window_ms = self._config.default_yield_ms

        result = await poll_until_settled(
            session,
            self._registry,
            proc.id,
            self._config,
            yield_ms=yield_window_ms,
            timeout_seconds=timeout_seconds,
            check_input_wait=True,
        )

        def _inject_hint(xml: str) -> str:
            if is_new_tab:
                return xml.replace(
                    "<command_result>",
                    f"<command_result>\n<hint>New terminal tab '{xml_text(terminal_name)}' created.</hint>",
                )
            return xml

        match result.outcome:
            case PollOutcome.PROCESS_EXIT:
                self._registry.mark_exited(
                    proc.id,
                    exit_code=None,
                    exit_signal=None,
                    status=ProcessStatus.COMPLETED,
                )
                session.set_expected_state(TerminalCommandStatus.IDLE)
                session.apply_outcome(result)
                return _inject_hint(
                    self._format_completed(
                        result.output_parts, result.elapsed_ms, terminal=terminal_name
                    )
                )
            case PollOutcome.PROMPT_DETECTED:
                self._registry.mark_exited(
                    proc.id,
                    exit_code=None,
                    exit_signal=None,
                    status=ProcessStatus.COMPLETED,
                )
                session.set_expected_state(TerminalCommandStatus.IDLE)
                session.apply_outcome(result)
                return _inject_hint(
                    self._format_completed(
                        result.output_parts, result.elapsed_ms, terminal=terminal_name
                    )
                )
            case PollOutcome.INPUT_WAIT:
                runtime = self._registry.running_runtime(proc.id)
                session.set_expected_state(TerminalCommandStatus.WAITING_INPUT)
                session.apply_outcome(result)
                return _inject_hint(
                    await self._format_running(
                        session,
                        result.output_parts,
                        runtime,
                        result.elapsed_ms,
                        detected_input_wait=True,
                        terminal=terminal_name,
                    )
                )
            case PollOutcome.LONG_RUNNING:
                runtime = self._registry.running_runtime(proc.id)
                session.set_expected_state(TerminalCommandStatus.LONG_RUNNING)
                session.apply_outcome(result)
                return _inject_hint(
                    await self._format_running(
                        session,
                        result.output_parts,
                        runtime,
                        result.elapsed_ms,
                        terminal=terminal_name,
                    )
                )
            case PollOutcome.STUCK:
                raw_idle_ms = int((time.monotonic() - session.last_byte_at) * 1000)
                session.set_expected_state(None)
                session.apply_outcome(result)
                return _inject_hint(
                    self._format_stuck(
                        result.output_parts, raw_idle_ms, result.elapsed_ms, terminal=terminal_name
                    )
                )
            case PollOutcome.PAGINATED:
                session.set_expected_state(TerminalCommandStatus.PAGINATED)
                session.apply_outcome(result)
                return _inject_hint(
                    self._format_paginated(
                        result.output_parts, result.elapsed_ms, terminal=terminal_name
                    )
                )
            case PollOutcome.YIELDED:
                session.set_expected_state(TerminalCommandStatus.EXECUTING)
                session.apply_outcome(result)
                return _inject_hint(
                    await self._format_running(
                        session,
                        result.output_parts,
                        None,
                        result.elapsed_ms,
                        terminal=terminal_name,
                    )
                )
            case PollOutcome.TIMED_OUT:
                await session.terminate()
                self._registry.mark_exited(
                    proc.id,
                    exit_code=None,
                    exit_signal="TIMEOUT",
                    status=ProcessStatus.TIMED_OUT,
                    timed_out=True,
                )
                session.set_expected_state(None)
                session.apply_outcome(result)
                return _inject_hint(
                    self._format_timed_out(
                        result.output_parts,
                        timeout_seconds,
                        result.elapsed_ms,
                        terminal=terminal_name,
                    )
                )

    # ------------------------------------------------------------------
    # XML formatting
    # ------------------------------------------------------------------

    @staticmethod
    def _format_completed(
        output_parts: list[str], elapsed_ms: int, *, terminal: str | None = None
    ) -> str:
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
                output,
                CommandResultStatus.WAITING_INPUT,
                elapsed_ms,
                terminal=terminal,
                idle_ms=idle_ms,
                message=message,
            )

        message = (
            "Command still executing. Use terminal current to check progress, "
            "process write/send_keys/paste for input."
        )
        xml = _build_command_xml(
            output,
            CommandResultStatus.EXECUTING,
            elapsed_ms,
            terminal=terminal,
            idle_ms=idle_ms,
            message=message,
        )

        if terminal_session.cursor_key_mode == CursorKeyMode.APPLICATION:
            segment = await terminal_session.current_segment()
            if segment and segment.text.strip():
                tui_text = sanitize_terminal_output(segment.text).rstrip()
                xml = xml.replace(
                    "</command_result>",
                    f"\n<tui_screen>{xml_text(tui_text)}</tui_screen>\n</command_result>",
                )
        else:
            segment = await terminal_session.current_segment()
            cursor = resolve_cursor_line(segment)
            if cursor.strip():
                cursor_text = sanitize_terminal_output(cursor).rstrip()
                xml = xml.replace(
                    "</command_result>",
                    f"\n<cursor_line>{xml_text(cursor_text)}</cursor_line>\n</command_result>",
                )

        return xml

    @staticmethod
    def _format_paginated(
        output_parts: list[str],
        elapsed_ms: int,
        *,
        terminal: str | None = None,
    ) -> str:
        raw = "".join(output_parts)
        output = sanitize_terminal_output(raw).rstrip()
        message = (
            "A pager (less/more) is active. "
            "Use 'process write' with 'q' to quit, Space for next page, "
            "or Enter for next line. Use 'terminal current' to see the full screen."
        )
        return _build_command_xml(
            output,
            CommandResultStatus.PAGINATED,
            elapsed_ms,
            terminal=terminal,
            message=message,
        )

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
            output,
            CommandResultStatus.STUCK,
            elapsed_ms,
            terminal=terminal,
            idle_ms=raw_idle_ms,
            message=message,
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
            output,
            CommandResultStatus.TIMED_OUT,
            elapsed_ms,
            terminal=terminal,
            message=message,
        )

    @staticmethod
    def _format_rejected(
        guard_result: TerminalGuardResult,
        *,
        terminal: str | None = None,
    ) -> str:
        snap = guard_result.snapshot
        parts = [
            "<command_result>",
            "<status>rejected</status>",
            f"<message>{xml_text(guard_result.message)}</message>",
        ]
        if terminal is not None:
            parts.append(f"<terminal>{xml_text(terminal)}</terminal>")
        parts.extend(
            [
                "<diagnostic>",
                f"<status>{snap.status.value}</status>",
                f"<idle_ms>{snap.idle_ms}</idle_ms>",
            ]
        )
        if snap.elapsed_ms is not None:
            parts.append(f"<elapsed_ms>{snap.elapsed_ms}</elapsed_ms>")
        if snap.cursor_line:
            parts.append(f"<cursor>{xml_text(snap.cursor_line)}</cursor>")
        if snap.last_output:
            parts.append(f"<last_output>{xml_text(snap.last_output)}</last_output>")
        if snap.suggestion:
            parts.append(f"<suggestion>{xml_text(snap.suggestion)}</suggestion>")
        parts.append("</diagnostic>")
        parts.append("</command_result>")
        return "\n".join(parts)
