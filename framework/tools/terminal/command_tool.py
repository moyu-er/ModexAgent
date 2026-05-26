"""CommandTool — execute commands in terminal sessions.

Three-tier completion detection:
  1. Process exit (authoritative)
  2. Prompt detection (Solution A — auxiliary completion for persistent shells)
  3. Timeout (kills process, returns partial output)
"""

from __future__ import annotations

import time

from framework.core.tool_manager import Tool
from framework.tools.terminal.config import TerminalRuntimeConfig
from framework.tools.terminal.managers import TerminalManagerBase
from framework.tools.terminal.process_registry import ProcessRegistry, RunningSessionRuntime
from framework.tools.terminal.prompt import sanitize_terminal_output
from framework.tools.terminal.pty_keys import CursorKeyMode
from framework.tools.terminal.session import TerminalSession
from framework.tools.terminal.types import ProcessStatus


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

        await session.write(command + "\r")

        timeout_seconds = self._config.default_command_timeout_seconds
        yield_window_ms = self._config.default_yield_ms

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
            if not await session.is_alive():
                self._registry.mark_exited(
                    proc.id,
                    exit_code=None,
                    exit_signal=None,
                    status=ProcessStatus.COMPLETED,
                )
                return self._format_completed(output_parts)

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
                            exit_code=None,
                            exit_signal=None,
                            status=ProcessStatus.COMPLETED,
                        )
                        return self._format_completed(output_parts)
                else:
                    prompt_stable_since = None

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
                return self._format_timed_out(output_parts, timeout_seconds)

            # 4. waiting_for_input hint
            runtime = self._registry.running_runtime(proc.id)
            if runtime is not None and runtime.waiting_for_input:
                return await self._format_running(session, output_parts, runtime)

            # 5. yield_ms elapsed
            if elapsed_ms >= yield_window_ms:
                return await self._format_running(session, output_parts, None)

    # ------------------------------------------------------------------
    # Formatting — natural language, no structured headers
    # ------------------------------------------------------------------

    @staticmethod
    def _format_completed(output_parts: list[str]) -> str:
        raw = "".join(output_parts)
        output = sanitize_terminal_output(raw).rstrip()
        return output or "(no output)"

    @staticmethod
    async def _format_running(
        terminal_session: TerminalSession,
        output_parts: list[str],
        runtime: RunningSessionRuntime | None,
    ) -> str:
        parts: list[str] = []
        raw = "".join(output_parts)
        output = sanitize_terminal_output(raw).rstrip()
        if output:
            parts.append(output)

        if runtime is not None and runtime.waiting_for_input:
            parts.append(
                "\nNo new output for {}s; this session may be waiting for input. "
                "Use process write, send_keys, submit, or paste to provide input.".format(
                    runtime.idle_ms // 1000
                )
            )
        else:
            parts.append(
                "\nCommand still running. Use process poll/log for status, "
                "process write/send_keys/paste for input."
            )

        if terminal_session.cursor_key_mode == CursorKeyMode.APPLICATION:
            segment = await terminal_session.current_segment()
            if segment and segment.text.strip():
                parts.append("\n" + segment.text.rstrip())
                parts.append("TUI program detected. Use process send_keys for interaction.")

        return "\n".join(parts)

    @staticmethod
    def _format_timed_out(output_parts: list[str], timeout_seconds: int) -> str:
        parts: list[str] = []
        raw = "".join(output_parts)
        output = sanitize_terminal_output(raw).rstrip()
        if output:
            parts.append(output)
        parts.append(
            f"\nCommand timed out after {timeout_seconds}s and was terminated. "
            "Partial output captured above."
        )
        return "\n".join(parts)
