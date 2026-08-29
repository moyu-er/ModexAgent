from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, assert_never

if TYPE_CHECKING:
    from modex_agent.core.message import ContentFormat

from modex_agent.core.tool_manager import Tool
from modex_agent.tools.terminal.config import TerminalRuntimeConfig
from modex_agent.tools.terminal.guard import TerminalGuardResult, check_process_writable
from modex_agent.tools.terminal.managers import TerminalManagerBase
from modex_agent.tools.terminal.poll_loop import (
    PollOutcome,
    PollResult,
    mark_exited_if_finished,
)
from modex_agent.tools.terminal.process_registry import (
    ProcessRegistry,
    ProcessSession,
)
from modex_agent.tools.terminal.prompt import sanitize_terminal_output
from modex_agent.tools.terminal.pty_keys import normalize_write_payload
from modex_agent.tools.terminal.session import TerminalSession
from modex_agent.tools.terminal.types import CommandResultStatus, ProcessStatus
from modex_agent.utils.xml import xml_text

__all__ = ["ProcessTool"]

logger = logging.getLogger(__name__)

_NO_RUNNING_MSG = (
    "[Error] No running command in the selected tab — the previous command "
    "already finished. Run a new command with bash."
)


async def _drain_terminal_after_action(
    terminal_session: TerminalSession,
    registry: ProcessRegistry,
    session_id: str,
    config: TerminalRuntimeConfig,
    *,
    command: str = "",
) -> tuple[str, PollResult]:
    """Read terminal output after write/submit. Uses shared poll_until_settled."""
    from modex_agent.tools.terminal.poll_loop import poll_until_settled
    from modex_agent.tools.terminal.prompt import sanitize_terminal_output

    result = await poll_until_settled(
        terminal_session,
        registry,
        session_id,
        config,
        check_input_wait=True,
        command=command,
    )

    if result.output_parts:
        output = sanitize_terminal_output("".join(result.output_parts)).rstrip()
    else:
        output = ""
    return output, result


def _build_process_xml(
    output: str,
    *,
    status: CommandResultStatus | None = None,
    message: str | None = None,
) -> str:
    parts = [
        "<process_result>",
        f"<output>\n{xml_text(output)}\n</output>",
    ]
    if status is not None:
        parts.append(f"<status>{xml_text(status.value)}</status>")
    if message is not None:
        parts.append(f"<message>{xml_text(message)}</message>")
    parts.append("</process_result>")
    return "\n".join(parts)


class ProcessTool(Tool):
    def __init__(
        self,
        registry: ProcessRegistry,
        manager: TerminalManagerBase,
        config: TerminalRuntimeConfig | None = None,
    ) -> None:
        super().__init__()
        self._registry = registry
        self._config = config or TerminalRuntimeConfig()
        self._manager = manager

    @property
    def name(self) -> str:
        return "process"

    @property
    def description(self) -> str:
        return (
            "Send one line of input to the running command in the currently selected terminal tab.\n"
            "Use it when bash returns waiting_input, or to interrupt a command.\n\n"
            '- data: the text to send. Special: "^C", "ctrl+c" or "\\x03" interrupts the running\n'
            "  command (Ctrl+C) — nothing is typed.\n"
            "- submit (default true): press Enter after the text. With submit=true a trailing newline\n"
            "  in data is dropped and exactly one Enter is sent. Set submit=false to send the text\n"
            '  exactly as typed with no Enter (e.g. a pager key: data="q" submit=false).\n\n'
            'Examples: data="y" answers a [y/n] prompt · data="q" submit=false quits a pager ·\n'
            'data="^C" interrupts the command.\n\n'
            "Returns the command's continued output once it settles (the same waiting_input hint\n"
            "may appear again). The 480s limit refreshes on each write.\n"
            "IMPORTANT: NEVER send a password without asking the user first."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "data": {
                    "type": "string",
                    "description": 'Text to send to the command\'s stdin. "^C", "ctrl+c" or "\\x03" interrupts the running command (nothing is typed).',
                },
                "submit": {
                    "type": "boolean",
                    "description": 'Press Enter after the text (default true). With submit=true a trailing newline in data is dropped and exactly one Enter is sent. Set false to send the text exactly as typed with no Enter (e.g. a pager key: data="q" submit=false).',
                    "default": True,
                },
            },
            "required": ["data"],
        }

    def result_metadata(self, result: Any) -> tuple[ContentFormat | None, list[str] | None]:
        """Declare XML truncation metadata for <process_result> output."""
        from modex_agent.tools.terminal.types import terminal_result_metadata

        return terminal_result_metadata(result)

    async def execute(self, **kwargs: Any) -> str:  # noqa: ANN401
        data = kwargs.get("data", "")
        submit = kwargs.get("submit", True)
        terminal_session, running, _finished = await self._resolve_terminal()
        if running is None:
            return _build_process_xml(_NO_RUNNING_MSG)

        if data.strip().lower() in {"^c", "ctrl+c", "\x03"}:
            await terminal_session.interrupt()
            self._registry.refresh_deadline(running.id)
            await asyncio.sleep(0.5)
            await terminal_session.refresh_output(timeout=1.0)
            output = sanitize_terminal_output(await terminal_session.last_command_output())
            segment = await terminal_session.current_segment()
            if not output:
                output = sanitize_terminal_output(segment.cursor_line or "(no output)")
            if segment.is_empty_prompt:
                self._registry.mark_exited(
                    running.id,
                    exit_code=None,
                    exit_signal="SIGINT",
                    status=ProcessStatus.KILLED,
                )
            return _build_process_xml(output)

        guard_result = await check_process_writable(
            terminal_session, config=self._config, registry=self._registry
        )
        if guard_result is not None:
            return self._format_write_rejected(guard_result)

        payload = normalize_write_payload(data, submit)
        await terminal_session.write(payload)
        self._registry.refresh_deadline(running.id)
        drained, result = await _drain_terminal_after_action(
            terminal_session,
            self._registry,
            running.id,
            self._config,
            command=running.command,
        )
        terminal_session.apply_outcome(result)
        match result.outcome:
            case PollOutcome.PROMPT_DETECTED | PollOutcome.PROCESS_EXIT:
                mark_exited_if_finished(self._registry, running.id, result.outcome)
                return _build_process_xml(drained or "(no output)")
            case PollOutcome.INPUT_WAIT:
                return _build_process_xml(
                    drained,
                    status=CommandResultStatus.WAITING_INPUT,
                    message=(
                        "The command may be waiting for input again. Answer it with "
                        "another write, or interrupt with ^C."
                    ),
                )
            case PollOutcome.TIMED_OUT:
                # Converged timeout contract (same as the watchdog and
                # CommandTool): close-first; mark only after the tab is
                # gone — a failed close leaves the session RUNNING so the
                # watchdog retries close+mark on its next tick.
                try:
                    await self._manager.close(terminal_session.name)
                except Exception:
                    logger.exception(
                        "process: failed to close timed-out terminal tab %s",
                        terminal_session.name,
                    )
                else:
                    self._registry.mark_exited(
                        running.id,
                        exit_code=None,
                        exit_signal="TIMEOUT",
                        status=ProcessStatus.TIMED_OUT,
                        timed_out=True,
                    )
                return _build_process_xml(
                    drained,
                    status=CommandResultStatus.TIMED_OUT,
                    message=(
                        f"The command hit the {self._config.command_deadline_seconds}s "
                        "limit and the tab was closed."
                    ),
                )
            case unreachable:
                assert_never(unreachable)

    async def _resolve_terminal(
        self,
    ) -> tuple[TerminalSession, ProcessSession | None, ProcessSession | None]:
        terminal_session = await self._manager.get_default()
        if terminal_session is None:
            raise ValueError("No default terminal session available")
        name = terminal_session.name
        running = self._registry.get_running_by_terminal(name)
        finished = self._registry.get_finished_by_terminal(name)
        return terminal_session, running, finished

    def _format_write_rejected(
        self,
        guard_result: TerminalGuardResult,
    ) -> str:

        snap = guard_result.snapshot
        parts = [
            "<process_result>",
            "<status>rejected</status>",
            f"<message>{xml_text(guard_result.message)}</message>",
        ]
        if snap.suggestion:
            parts.append(f"<suggestion>{xml_text(snap.suggestion)}</suggestion>")
        parts.append("</process_result>")
        return "\n".join(parts)
