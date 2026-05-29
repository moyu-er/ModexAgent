"""CommandTool — execute commands in terminal sessions.

Three-tier completion detection:
  1. Process exit (authoritative)
  2. Prompt detection (auxiliary completion for persistent shells)
  3. Timeout (kills process, returns partial output)

Returns structured <command_result> XML with CommandResultStatus.
"""

from __future__ import annotations

import asyncio
import time
from xml.sax.saxutils import escape as xml_escape

from framework.core.tool_manager import Tool
from framework.tools.terminal.config import TerminalRuntimeConfig
from framework.tools.terminal.managers import TerminalManagerBase
from framework.tools.terminal.process_registry import ProcessRegistry, RunningSessionRuntime
from framework.tools.terminal.prompt import (
    detect_pager_entry,
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
    idle_ms: int | None = None,
    pages_scrolled: int | None = None,
    truncated: bool | None = None,
    message: str | None = None,
) -> str:
    """Build a <command_result> XML string."""
    parts: list[str] = [
        "<command_result>",
        f"<output>{xml_escape(output)}</output>",
        f"<status>{status.value}</status>",
        f"<elapsed_ms>{elapsed_ms}</elapsed_ms>",
    ]
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
        return "command"

    @property
    def description(self) -> str:
        return (
            "Execute a command in a persistent terminal session. "
            "The shell is stateful: cd, environment variables, venv/nvm activations, "
            "and SSH connections persist across commands. Do NOT re-run setup commands "
            "(cd, source, export, etc.) that were already executed in this session.\n"
            "IMPORTANT: If a command asks for a password, STOP and ask the user. "
            "NEVER guess or invent passwords. Use 'process write submit=true' only "
            "after the user provides the password.\n"
            "If the command completes quickly you get output directly. "
            "If it keeps running, use the process tool for follow-up."
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
        await session.ensure_started()

        proc = self._registry.create(
            command=command,
            terminal=session.name,
            cwd=None,
            pid=None,
        )

        await session.submit_command(command)

        timeout_seconds = self._config.default_command_timeout_seconds
        yield_window_ms = self._config.default_yield_ms

        start = time.monotonic()
        last_output_time = start
        output_parts: list[str] = []
        output_received = False
        prompt_stable_since: float | None = None

        while True:
            elapsed_ms = int((time.monotonic() - start) * 1000)

            # Read output
            read = await session.poll_once(timeout=0.05)
            if read.stdout:
                self._registry.append_output(proc.id, "stdout", read.stdout)
                output_parts.append(read.stdout)
                output_received = True
                prompt_stable_since = None
                last_output_time = time.monotonic()
            if read.stderr:
                self._registry.append_output(proc.id, "stderr", read.stderr)
                output_parts.append(read.stderr)

            # 1. Process exit (authoritative)
            if not await session.is_alive():
                self._registry.mark_exited(
                    proc.id,
                    exit_code=None,
                    exit_signal=None,
                    status=ProcessStatus.COMPLETED,
                )
                return self._format_completed(output_parts, elapsed_ms)

            # 2. Prompt detection (auxiliary completion)
            if output_received:
                segment = await session.current_segment()
                if segment.is_empty_prompt:
                    if prompt_stable_since is None:
                        prompt_stable_since = time.monotonic()
                    elif (
                        (time.monotonic() - prompt_stable_since) * 1000
                        >= self._config.prompt_stabilize_ms
                    ):
                        self._registry.mark_exited(
                            proc.id,
                            exit_code=None,
                            exit_signal=None,
                            status=ProcessStatus.COMPLETED,
                        )
                        return self._format_completed(output_parts, elapsed_ms)
                else:
                    prompt_stable_since = None

            # 2.5 Pager detection
            if output_received and not read.stdout:
                idle_elapsed = time.monotonic() - last_output_time
                if idle_elapsed >= self._config.pager_idle_detect_seconds:
                    segment = await session.current_segment()
                    cursor = resolve_cursor_line(segment)
                    if (
                        not segment.is_empty_prompt
                        and detect_pager_entry(cursor)
                    ):
                        output_parts, pages = await self._auto_scroll_pager(
                            session, output_parts, proc.id,
                        )
                        self._registry.mark_exited(
                            proc.id,
                            exit_code=None,
                            exit_signal=None,
                            status=ProcessStatus.COMPLETED,
                        )
                        elapsed_ms = int((time.monotonic() - start) * 1000)
                        total_chars = sum(len(p) for p in output_parts)
                        return self._format_paginated(
                            output_parts, pages, elapsed_ms,
                            total_chars, self._config.pager_auto_scroll_max_chars,
                        )

            # 3. Timeout (kills process)
            if elapsed_ms >= timeout_seconds * 1000:
                await session.terminate()
                self._registry.mark_exited(
                    proc.id,
                    exit_code=None,
                    exit_signal="TIMEOUT",
                    status=ProcessStatus.TIMED_OUT,
                    timed_out=True,
                )
                return self._format_timed_out(output_parts, timeout_seconds, elapsed_ms)

            # 4. waiting_for_input hint
            runtime = self._registry.running_runtime(proc.id)
            if runtime is not None and runtime.waiting_for_input:
                return await self._format_running(session, output_parts, runtime, elapsed_ms)

            # 5. yield_ms elapsed
            if elapsed_ms >= yield_window_ms:
                return await self._format_running(session, output_parts, None, elapsed_ms)

    # ------------------------------------------------------------------
    # XML formatting
    # ------------------------------------------------------------------

    @staticmethod
    def _format_completed(output_parts: list[str], elapsed_ms: int) -> str:
        raw = "".join(output_parts)
        output = sanitize_terminal_output(raw).rstrip()
        return _build_command_xml(
            output or "(no output)",
            CommandResultStatus.COMPLETED,
            elapsed_ms,
        )

    @staticmethod
    async def _format_running(
        terminal_session: TerminalSession,
        output_parts: list[str],
        runtime: RunningSessionRuntime | None,
        elapsed_ms: int,
    ) -> str:
        raw = "".join(output_parts)
        output = sanitize_terminal_output(raw).rstrip()
        idle_ms = runtime.idle_ms if runtime else None

        if runtime is not None and runtime.waiting_for_input:
            message = (
                f"No new output for {runtime.idle_ms // 1000}s; this session may be "
                "waiting for input. Use process write, send_keys, submit, or paste "
                "to provide input."
            )
            return _build_command_xml(
                output, CommandResultStatus.INPUT_WAIT, elapsed_ms,
                idle_ms=idle_ms, message=message,
            )

        message = (
            "Command still running. Use process poll/log for status, "
            "process write/send_keys/paste for input."
        )
        xml = _build_command_xml(
            output, CommandResultStatus.RUNNING, elapsed_ms,
            idle_ms=idle_ms, message=message,
        )

        if terminal_session.cursor_key_mode == CursorKeyMode.APPLICATION:
            segment = await terminal_session.current_segment()
            if segment and segment.text.strip():
                tui_text = sanitize_terminal_output(segment.text).rstrip()
                xml = xml.replace(
                    "</command_result>",
                    f"\n<tui_screen>{xml_escape(tui_text)}</tui_screen>\n</command_result>",
                )

        return xml

    @staticmethod
    def _format_timed_out(output_parts: list[str], timeout_seconds: int, elapsed_ms: int) -> str:
        raw = "".join(output_parts)
        output = sanitize_terminal_output(raw).rstrip()
        message = (
            f"Command timed out after {timeout_seconds}s and was terminated. "
            "Partial output captured above."
        )
        return _build_command_xml(
            output, CommandResultStatus.TIMED_OUT, elapsed_ms,
            message=message,
        )

    @staticmethod
    def _format_paginated(
        output_parts: list[str],
        pages_scrolled: int,
        elapsed_ms: int,
        total_chars: int,
        max_chars: int,
    ) -> str:
        raw = "".join(output_parts)
        output = sanitize_terminal_output(raw).rstrip()
        truncated = total_chars >= max_chars
        message = (
            "Output was displayed through a pager and automatically scrolled. "
            'If content was cut off, use process send_keys keys=[" "] to continue '
            'scrolling, or process send_keys keys=["q"] to exit the pager.'
        )
        return _build_command_xml(
            output, CommandResultStatus.PAGINATED, elapsed_ms,
            pages_scrolled=pages_scrolled,
            truncated=truncated,
            message=message,
        )

    # ------------------------------------------------------------------
    # Pager auto-scroll
    # ------------------------------------------------------------------

    async def _auto_scroll_pager(
        self,
        session: TerminalSession,
        initial_output: list[str],
        proc_id: str,
    ) -> tuple[list[str], int]:
        """Auto-scroll through a pager by sending space characters.

        Returns the collected output parts and number of pages scrolled.
        """
        output_parts = list(initial_output)
        total_chars = sum(len(p) for p in output_parts)
        pages_scrolled = 0
        idle_timeout = self._config.pager_idle_detect_seconds

        for _ in range(self._config.pager_auto_scroll_max_pages):
            await session.write(" ")

            new_output = False
            deadline = time.monotonic() + idle_timeout
            while time.monotonic() < deadline:
                read = await session.poll_once(timeout=0.3)
                if read.stdout:
                    self._registry.append_output(proc_id, "stdout", read.stdout)
                    output_parts.append(read.stdout)
                    total_chars += len(read.stdout)
                    new_output = True
                    break
                if not await session.is_alive():
                    break

            if not new_output:
                break

            pages_scrolled += 1
            if total_chars >= self._config.pager_auto_scroll_max_chars:
                break

            segment = await session.current_segment()
            if segment.is_empty_prompt:
                return output_parts, pages_scrolled

        # Exit the pager
        await session.write("q")
        await asyncio.sleep(0.5)
        while True:
            read = await session.poll_once(timeout=0.3)
            if not read.stdout:
                break
            output_parts.append(read.stdout)

        return output_parts, pages_scrolled
