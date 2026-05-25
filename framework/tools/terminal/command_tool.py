"""CommandTool — execute commands in named terminal sessions.

Replaces ShellTool with a structured result format and three-tier
completion detection:
  1. Process exit (authoritative)
  2. Prompt detection (Solution A — auxiliary completion for persistent shells)
  3. Timeout (kills process)
"""

from __future__ import annotations

import time
from typing import Any

from framework.core.tool_manager import Tool
from framework.tools.terminal.config import (
    TerminalRuntimeConfig,
    resolve_command_timeout,
    resolve_yield_ms,
)
from framework.tools.terminal.managers import TerminalManagerProtocol
from framework.tools.terminal.process_registry import ProcessRegistry
from framework.tools.terminal.pty_keys import CursorKeyMode
from framework.tools.terminal.session import TerminalSession
from framework.tools.terminal.types import ProcessStatus


class CommandTool(Tool):
    """Start a command in a named terminal session.

    If the command finishes before the yield window, returns
    status=completed with output.  If still running after yield_ms,
    returns status=running with a session_id for follow-up via the
    process tool.
    """

    def __init__(
        self,
        manager: TerminalManagerProtocol,
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
            "Start a command in a named terminal session. If the command finishes "
            "before the yield window, returns status=completed with output. If still "
            "running after yield_ms, returns status=running with a session_id for "
            "follow-up via the process tool. "
            "Use process poll/log/write/submit/send_keys/paste/interrupt/kill for follow-up."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The command to execute",
                },
                "terminal": {
                    "type": "string",
                    "description": "Named terminal tab (default: manager default)",
                },
                "workdir": {
                    "type": "string",
                    "description": "Working directory",
                },
                "env": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                    "description": "Environment overrides",
                },
                "timeout": {
                    "type": "number",
                    "description": "Hard timeout in seconds",
                },
                "yield_ms": {
                    "type": "number",
                    "description": "Foreground wait window in ms before returning running",
                },
                "background": {
                    "type": "boolean",
                    "description": "Return running immediately",
                },
                "pty": {
                    "type": "boolean",
                    "description": "Use PTY (always true for now)",
                },
            },
            "required": ["command"],
        }

    async def execute(
        self,
        command: str,
        terminal: str | None = None,
        workdir: str | None = None,
        env: dict[str, str] | None = None,
        timeout: int | None = None,
        yield_ms: int | None = None,
        background: bool = False,
        pty: bool = True,
        **_kwargs: object,
    ) -> str:
        session = await self._manager.get_or_create(terminal, workdir=workdir)
        await session.ensure_started()

        proc = self._registry.create(
            command=command,
            terminal=session.name,
            cwd=workdir,
            pid=None,
        )

        # Use readline-style ending (\r) — the shell translates to \n internally.
        await session.write(command + "\r")

        inner_timeout = resolve_command_timeout(timeout, self._config)
        yield_window_ms = 0 if background else resolve_yield_ms(yield_ms, self._config)

        start = time.monotonic()
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
            if read.stderr:
                self._registry.append_output(proc.id, "stderr", read.stderr)
                output_parts.append(read.stderr)

            # 1. Process exit (authoritative)
            alive = await session._backend.is_alive()
            if not alive:
                self._registry.mark_exited(
                    proc.id,
                    exit_code=0,
                    exit_signal=None,
                    status=ProcessStatus.COMPLETED,
                )
                duration_ms = int((time.monotonic() - start) * 1000)
                return self._format_completed(proc.id, session.name, output_parts, duration_ms)

            # 2. Prompt detection (Solution A — auxiliary completion)
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
                            exit_code=0,
                            exit_signal=None,
                            status=ProcessStatus.COMPLETED,
                        )
                        duration_ms = int((time.monotonic() - start) * 1000)
                        return self._format_completed(
                            proc.id, session.name, output_parts, duration_ms
                        )
                else:
                    prompt_stable_since = None

            # 3. Timeout (kills process)
            if elapsed_ms >= inner_timeout * 1000:
                await session.terminate()
                self._registry.mark_exited(
                    proc.id,
                    exit_code=None,
                    exit_signal="TIMEOUT",
                    status=ProcessStatus.TIMED_OUT,
                    timed_out=True,
                    failure_kind="overall-timeout",
                )
                duration_ms = int((time.monotonic() - start) * 1000)
                return self._format_timed_out(
                    proc.id, session.name, output_parts, duration_ms, inner_timeout
                )

            # 4. waiting_for_input hint
            runtime = self._registry.running_runtime(proc.id)
            if runtime is not None and runtime.waiting_for_input:
                duration_ms = int((time.monotonic() - start) * 1000)
                return await self._format_running(
                    proc.id, session, output_parts, duration_ms, runtime
                )

            # 5. yield_ms elapsed
            if not background and elapsed_ms >= yield_window_ms:
                duration_ms = int((time.monotonic() - start) * 1000)
                return await self._format_running(
                    proc.id, session, output_parts, duration_ms, None
                )

    # ------------------------------------------------------------------
    # Formatting helpers
    # ------------------------------------------------------------------

    def _format_completed(
        self,
        session_id: str,
        terminal: str,
        output_parts: list[str],
        duration_ms: int,
    ) -> str:
        output = "".join(output_parts).rstrip()
        lines = [
            "[Command Result]",
            "status: completed",
            f"session_id: {session_id}",
            f"terminal: {terminal}",
            f"duration_ms: {duration_ms}",
        ]
        if output:
            lines.extend(["", "[Output]", output])
        return "\n".join(lines)

    async def _format_running(
        self,
        session_id: str,
        terminal_session: TerminalSession,
        output_parts: list[str],
        duration_ms: int,
        runtime: Any | None,
    ) -> str:
        output = "".join(output_parts).rstrip()
        lines = [
            "[Command Result]",
            "status: running",
            f"session_id: {session_id}",
            f"terminal: {terminal_session.name}",
            f"duration_ms: {duration_ms}",
        ]
        if output:
            lines.extend(["", "[Output]", output])
        state_lines = ["", "[State]"]
        if runtime is not None:
            state_lines.append(f"stdin_writable: {str(runtime.stdin_writable).lower()}")
            state_lines.append(f"waiting_for_input: {str(runtime.waiting_for_input).lower()}")
            state_lines.append(f"idle_ms: {runtime.idle_ms}")
            if runtime.waiting_for_input:
                state_lines.append(
                    "hint: Process appears to be waiting for input. Use process write/submit to respond."
                )
            elif runtime.output_velocity.is_active:
                state_lines.append("output_velocity: active")
                state_lines.append("hint: Output is still being produced. Poll again in a few seconds.")
        lines.extend(state_lines)

        # Screen snapshot for TUI programs
        if terminal_session.cursor_key_mode == CursorKeyMode.APPLICATION:
            segment = await terminal_session.current_segment()
            if segment and segment.text.strip():
                lines.extend(["", "[Screen]"])
                lines.append(segment.text.rstrip())
                lines.append("hint: TUI program detected. Use send_keys for interaction.")

        return "\n".join(lines)

    def _format_timed_out(
        self,
        session_id: str,
        terminal: str,
        output_parts: list[str],
        duration_ms: int,
        timeout_seconds: int,
    ) -> str:
        output = "".join(output_parts).rstrip()
        lines = [
            "[Command Result]",
            "status: timed_out",
            f"session_id: {session_id}",
            f"terminal: {terminal}",
            f"duration_ms: {duration_ms}",
            "timed_out: true",
            "",
            "[Output]",
        ]
        if output:
            lines.append(output)
        lines.extend([
            "",
            "[State]",
            "message: Timed out after {}s; process terminated. Partial output captured above.".format(
                timeout_seconds
            ),
        ])
        return "\n".join(lines)
