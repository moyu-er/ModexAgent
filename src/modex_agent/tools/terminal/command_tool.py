"""CommandTool — execute commands in terminal sessions."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, ClassVar, assert_never

if TYPE_CHECKING:
    from modex_agent.core.message import ContentFormat

from modex_agent.core.tool_manager import ExclusiveTool
from modex_agent.tools.terminal.config import TerminalRuntimeConfig
from modex_agent.tools.terminal.guard import TerminalGuardResult, check_command_writable
from modex_agent.tools.terminal.managers import TerminalManagerBase
from modex_agent.tools.terminal.poll_loop import mark_exited_if_finished
from modex_agent.tools.terminal.process_registry import ProcessRegistry
from modex_agent.tools.terminal.prompt import sanitize_terminal_output
from modex_agent.tools.terminal.types import CommandResultStatus, ProcessStatus
from modex_agent.utils.xml import xml_text

logger = logging.getLogger(__name__)


def _build_command_xml(
    output: str,
    status: CommandResultStatus,
    elapsed_ms: int,
    *,
    no_output_ms: int | None = None,
    message: str | None = None,
    hint: str | None = None,
) -> str:
    parts: list[str] = ["<command_result>"]
    if hint is not None:
        parts.append(f"<hint>{xml_text(hint)}</hint>")
    parts.extend(
        [
            f"<output>\n{xml_text(output)}\n</output>",
            f"<status>{status.value}</status>",
            f"<duration_ms>{elapsed_ms}</duration_ms>",
        ]
    )
    if no_output_ms is not None:
        parts.append(f"<no_output_ms>{no_output_ms}</no_output_ms>")
    if message is not None:
        parts.append(f"<message>{xml_text(message)}</message>")
    parts.append("</command_result>")
    return "\n".join(parts)


class CommandTool(ExclusiveTool):
    """Execute a command in the default terminal session."""

    cancel_note: ClassVar[str | None] = None

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

    @property
    def name(self) -> str:
        return "bash"

    @property
    def description(self) -> str:
        return (
            "Run a shell command in a persistent terminal session (the currently selected tab).\n"
            "Working directory, environment variables, and background processes persist between calls in the same tab.\n\n"
            "The command runs until one of:\n"
            "- completed — the command finished. Output is included.\n"
            "- waiting_input — no output for ~10s and the session MAY be waiting for input (a prompt,\n"
            "  password request, pager, or question). This is a guess, not a fact — the command may\n"
            "  simply be slow. Judge from the output above: answer it with the process tool, interrupt\n"
            "  with ^C via process, or keep waiting. The command keeps running while you decide\n"
            "  (480s total limit).\n"
            "- timed_out — the command hit the 480s limit. The tab was closed and the shell reset:\n"
            "  working directory and environment variables are NOT preserved. Run long-lived commands\n"
            "  in the background (e.g. 'sleep 10 &').\n\n"
            "Do NOT re-run setup commands (cd, source, export) that already ran in this tab.\n"
            "If a previous command is still running or waiting, new commands are rejected — interact\n"
            "with it via the process tool first. Use 'terminal list' to see tabs; 'terminal open/select'\n"
            "to switch. IMPORTANT: if a command asks for a password, STOP and ask the user."
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

    def result_metadata(self, result: Any) -> tuple[ContentFormat | None, list[str] | None]:
        """Declare XML truncation metadata for <command_result> output."""
        from modex_agent.tools.terminal.types import terminal_result_metadata

        return terminal_result_metadata(result)

    async def execute(self, command: str = "", **_kwargs: object) -> str:
        from modex_agent.runtime.env_context import _modex_env
        from modex_agent.tools.terminal.env import build_full_env
        from modex_agent.tools.terminal.poll_loop import PollOutcome, poll_until_settled

        overrides = _modex_env.get()
        session = await self._manager.get_default()
        terminal_name = session.name
        is_new_tab = not session.backend_started
        previous_finished = self._registry.get_finished_by_terminal(terminal_name)

        guard_result = await check_command_writable(session, config=self._config)
        if guard_result is not None:
            return self._format_rejected(guard_result)

        await session.ensure_started(env=build_full_env(overrides) if overrides else None)
        proc = self._registry.create(
            command=command,
            terminal=terminal_name,
            cwd=None,
            pid=None,
        )
        await session.submit_command(command)
        result = await poll_until_settled(
            session,
            self._registry,
            proc.id,
            self._config,
            command=command,
            check_input_wait=True,
        )

        hint: str | None = None
        if is_new_tab:
            hint = f"New terminal tab '{terminal_name}' created."
            if (
                previous_finished is not None
                and previous_finished.status is ProcessStatus.TIMED_OUT
            ):
                hint += (
                    f" Previous tab timed out after {self._config.command_deadline_seconds}s "
                    "and was closed."
                )

        match result.outcome:
            case PollOutcome.PROMPT_DETECTED | PollOutcome.PROCESS_EXIT:
                mark_exited_if_finished(self._registry, proc.id, result.outcome)
                session.apply_outcome(result)
                return self._format_completed(result.output_parts, result.elapsed_ms, hint=hint)
            case PollOutcome.INPUT_WAIT:
                session.apply_outcome(result)
                idle_ms = self._registry.idle_ms(proc.id)
                assert idle_ms is not None
                return self._format_waiting_input(
                    result.output_parts,
                    result.elapsed_ms,
                    idle_ms,
                    hint=hint,
                )
            case PollOutcome.TIMED_OUT:
                session.apply_outcome(result)
                # Converged timeout contract (same as the watchdog and
                # ProcessTool): close-first; mark only after the tab is
                # gone — a failed close leaves the session RUNNING so the
                # watchdog retries close+mark on its next tick.
                try:
                    await self._manager.close(terminal_name)
                except Exception:
                    logger.exception(
                        "bash: failed to close timed-out terminal tab %s",
                        terminal_name,
                    )
                else:
                    self._registry.mark_exited(
                        proc.id,
                        exit_code=None,
                        exit_signal="TIMEOUT",
                        status=ProcessStatus.TIMED_OUT,
                        timed_out=True,
                    )
                return self._format_timed_out(result.output_parts, result.elapsed_ms, hint=hint)
            case unreachable:
                assert_never(unreachable)

    async def on_cancel(self) -> None:
        """Interrupt the running command while preserving its terminal tab."""
        from modex_agent.tools.terminal.process_tool import interrupt_running_command

        session = await self._manager.get_default()
        running = self._registry.get_running_by_terminal(session.name)
        if running is not None:
            await interrupt_running_command(session, running, self._registry)

    @staticmethod
    def _format_completed(
        output_parts: list[str],
        elapsed_ms: int,
        *,
        hint: str | None,
    ) -> str:
        output = sanitize_terminal_output("".join(output_parts)).rstrip()
        return _build_command_xml(
            output or "(no output)",
            CommandResultStatus.COMPLETED,
            elapsed_ms,
            hint=hint,
        )

    def _format_waiting_input(
        self,
        output_parts: list[str],
        elapsed_ms: int,
        idle_ms: int,
        *,
        hint: str | None,
    ) -> str:
        output = sanitize_terminal_output("".join(output_parts)).rstrip()
        message = (
            f"No output for {idle_ms // 1000}s — the command MAY be waiting for input "
            "(prompt, password, pager).\n"
            "Judge from the output above: answer it with the process tool, interrupt with ^C, or wait.\n"
            f"The command keeps running ({self._config.command_deadline_seconds}s limit)."
        )
        return _build_command_xml(
            output,
            CommandResultStatus.WAITING_INPUT,
            elapsed_ms,
            no_output_ms=idle_ms,
            message=message,
            hint=hint,
        )

    def _format_timed_out(
        self,
        output_parts: list[str],
        elapsed_ms: int,
        *,
        hint: str | None,
    ) -> str:
        output = sanitize_terminal_output("".join(output_parts)).rstrip()
        message = (
            f"The command hit the {self._config.command_deadline_seconds}s limit and the tab was "
            "closed. The shell was reset: working\n"
            "directory and environment variables are NOT preserved. Partial output above. Run\n"
            "long-lived commands in the background, or answer prompts promptly via the process tool."
        )
        return _build_command_xml(
            output or "(no output)",
            CommandResultStatus.TIMED_OUT,
            elapsed_ms,
            message=message,
            hint=hint,
        )

    @staticmethod
    def _format_rejected(guard_result: TerminalGuardResult) -> str:
        snap = guard_result.snapshot
        parts = [
            "<command_result>",
            "<status>rejected</status>",
            f"<message>{xml_text(guard_result.message)}</message>",
        ]
        if snap.suggestion:
            parts.append(f"<suggestion>{xml_text(snap.suggestion)}</suggestion>")
        parts.append("</command_result>")
        return "\n".join(parts)
