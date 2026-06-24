from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from modex_agent.core.tool_manager import Tool
from modex_agent.tools.terminal.config import TerminalRuntimeConfig
from modex_agent.tools.terminal.guard import TerminalGuardResult, check_process_writable
from modex_agent.tools.terminal.managers import TerminalManagerBase
from modex_agent.tools.terminal.process_registry import (
    ProcessRegistry,
    ProcessSession,
)
from modex_agent.tools.terminal.pty_keys import (
    ENTER_KEY,
    CursorKeyMode,
    ProcessAction,
    encode_key_sequence,
    encode_paste,
    needs_cursor_mode,
)
from modex_agent.tools.terminal.session import TerminalSession
from modex_agent.tools.terminal.types import ProcessStatus
from modex_agent.utils.xml import xml_text

# Re-export ProcessAction for test / external use
__all__ = [
    "ProcessTool",
    "SendKeysParams",
    "PasteParams",
    "WriteParams",
    "ProcessAction",
]

# Shorthand constants to avoid hardcoding action strings everywhere.
_A = ProcessAction


@dataclass(frozen=True)
class SendKeysParams:
    keys: list[str] | None = None
    hex_bytes: list[str] | None = None
    literal: str | None = None


@dataclass(frozen=True)
class PasteParams:
    text: str = ""


@dataclass(frozen=True)
class WriteParams:
    data: str = ""
    submit: bool = False


async def _drain_terminal_after_action(
    terminal_session: TerminalSession,
    registry: ProcessRegistry,
    session_id: str,
    config: TerminalRuntimeConfig,
) -> str:
    """Read terminal output after write/submit. Uses shared poll_until_settled."""
    from modex_agent.tools.terminal.poll_loop import poll_until_settled
    from modex_agent.tools.terminal.prompt import sanitize_terminal_output

    result = await poll_until_settled(
        terminal_session,
        registry,
        session_id,
        config,
        yield_ms=config.default_yield_ms,
        timeout_seconds=config.default_command_timeout_seconds,
        check_input_wait=False,
    )

    if result.output_parts:
        return sanitize_terminal_output("".join(result.output_parts)).rstrip()
    return ""


def _build_process_xml(
    action: str,
    output: str,
    *,
    terminal_name: str | None = None,
    session_id: str | None = None,
    status: str | None = None,
    idle_ms: int | None = None,
    bytes_written: int | None = None,
    sessions_xml: str | None = None,
) -> str:
    parts = [
        "<process_result>",
        f"<action>{xml_text(action)}</action>",
        f"<output>{xml_text(output)}</output>",
    ]
    if terminal_name is not None:
        parts.append(f"<terminal>{xml_text(terminal_name)}</terminal>")
    if session_id is not None:
        parts.append(f"<session_id>{xml_text(session_id)}</session_id>")
    if status is not None:
        parts.append(f"<status>{xml_text(status)}</status>")
    if idle_ms is not None:
        parts.append(f"<idle_ms>{idle_ms}</idle_ms>")
    if bytes_written is not None:
        parts.append(f"<bytes_written>{bytes_written}</bytes_written>")
    if sessions_xml is not None:
        parts.append(f"<sessions>{sessions_xml}</sessions>")
    parts.append("</process_result>")
    return "\n".join(parts)


class ProcessTool(Tool):
    """Interact with a running command in the CURRENTLY SELECTED terminal tab.

    Use 'terminal current' to see output and status.
    Use 'terminal list' to see all sessions.
    Use write/send_keys/submit/paste/interrupt/kill for input or intervention.

    Actions:
      write      — send text to the running command's stdin (use submit=true for Enter)
      submit     — send Enter key to stdin (confirm a prompt after write)
      send_keys  — send key sequences: arrows, c-c (Ctrl+C), escape, tab, f1-f12
      paste      — paste multi-line text with bracketed-paste wrapping
      interrupt  — send Ctrl+C to stop the command
      kill       — forcefully terminate the command
      clear      — remove a finished session record
      remove     — kill (if running) and remove the session
    """

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
            "Interact with a running command in the CURRENTLY SELECTED terminal tab.\n"
            "Use 'terminal current' to see output and status.\n"
            "Use 'terminal list' to see all sessions.\n\n"
            "Actions:\n"
            f"  {_A.WRITE}     -- send text to the command's stdin\n"
            f"  {_A.SUBMIT}    -- send Enter key to stdin (confirm a prompt after write)\n"
            f"  {_A.SEND_KEYS} -- send key sequences: arrows, c-c (Ctrl+C), escape, tab, f1-f12\n"
            f"  {_A.PASTE}     -- paste multi-line text\n"
            f"  {_A.INTERRUPT} -- send Ctrl+C to stop the command\n"
            f"  {_A.KILL}      -- forcefully terminate the command\n"
            f"  {_A.CLEAR}     -- remove a finished session record\n"
            f"  {_A.REMOVE}    -- kill (if running) and remove the session\n\n"
            "Batch input: set repeat=N (default 1) to send the same input N times. "
            "Useful for scrolling a pager: process write data=' ' repeat=5.\n\n"
            "IMPORTANT: NEVER write a password without asking the user first. "
            "If a command prompts for a password, STOP and ask the user. "
            "Only use write for passwords after the user explicitly provides one.\n"
            "After providing input, use 'terminal current' to check the result.\n"
            f'To answer a prompt: process {_A.WRITE} data="USER_PROVIDED_VALUE" submit=true.\n'
            "Use send_keys for TUI programs (arrows, escape, Ctrl+C).\n"
            "Use interrupt/kill to stop commands."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [a.value for a in ProcessAction],
                    "description": " | ".join(a.value for a in ProcessAction),
                },
                "data": {
                    "type": "string",
                    "description": f"Text to send to stdin ({_A.WRITE} action). Include \\n for newline if needed.",
                },
                "keys": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": f"Key tokens: arrows, c-c (Ctrl+C), escape, enter, tab, backspace, f1-f12, hex:NN ({_A.SEND_KEYS} action)",
                },
                "hex": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": f'Hex bytes for {_A.SEND_KEYS}, e.g. ["1b", "0d"]',
                },
                "literal": {
                    "type": "string",
                    "description": f"Literal text to send with {_A.SEND_KEYS}",
                },
                "text": {
                    "type": "string",
                    "description": f"Multi-line text to paste ({_A.PASTE} action)",
                },
                "submit": {
                    "type": "boolean",
                    "description": f"Send Enter key after writing ({_A.WRITE} action). Use for passwords, y/n confirmations.",
                },
                "repeat": {
                    "type": "integer",
                    "description": (
                        "Repeat the input this many times (default 1, max 100). "
                        "Useful for batch-scrolling a pager: process write data=' ' repeat=5 sends Space 5 times. "
                        "Stops early if the pager exits or output stops changing."
                    ),
                    "default": 1,
                    "minimum": 1,
                    "maximum": 100,
                },
            },
            "required": ["action"],
        }

    # ------------------------------------------------------------------
    # execute dispatch
    # ------------------------------------------------------------------

    async def execute(self, **kwargs: Any) -> str:  # noqa: ANN401
        action_raw = kwargs.get("action", "")
        try:
            action = ProcessAction(action_raw)
        except ValueError:
            valid = ", ".join(a.value for a in ProcessAction)
            return f"[Error] Unknown action: {action_raw}. Valid: {valid}"

        match action:
            case ProcessAction.WRITE:
                return await self._do_write(
                    WriteParams(
                        data=kwargs.get("data", ""),
                        submit=kwargs.get("submit", True),
                    ),
                    repeat=int(kwargs.get("repeat", 1)),
                )
            case ProcessAction.SUBMIT:
                return await self._do_submit(repeat=int(kwargs.get("repeat", 1)))
            case ProcessAction.SEND_KEYS:
                return await self._do_send_keys(
                    SendKeysParams(
                        keys=kwargs.get("keys"),
                        hex_bytes=kwargs.get("hex"),
                        literal=kwargs.get("literal"),
                    ),
                )
            case ProcessAction.PASTE:
                return await self._do_paste(
                    PasteParams(text=kwargs.get("text", "")),
                )
            case ProcessAction.INTERRUPT:
                return await self._do_interrupt()
            case ProcessAction.KILL:
                return await self._do_kill()
            case ProcessAction.CLEAR:
                return await self._do_clear()
            case ProcessAction.REMOVE:
                return await self._do_remove()

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

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

    async def _batch_write_with_early_stop(
        self,
        terminal_session: TerminalSession,
        payload: str,
        repeat: int,
    ) -> tuple[str, int]:
        """Send *payload* up to *repeat* times with early-stop detection.

        Stops when: terminal reaches idle prompt, or two consecutive reads
        produce unchanged output (pager has exited / command finished).

        Returns (accumulated terminal output, actual repetitions performed).
        """
        from modex_agent.tools.terminal.prompt import sanitize_terminal_output

        repeat = min(repeat, 100)
        accumulated_parts: list[str] = []
        last_len = 0
        empty_streak = 0
        actual = 0

        for _ in range(repeat):
            await terminal_session.write(payload)
            actual += 1

            read = await terminal_session.poll_once(timeout=0.3)
            if read.raw:
                clean = sanitize_terminal_output(read.raw)
                accumulated_parts.append(clean)
                if len(clean) == last_len:
                    empty_streak += 1
                    if empty_streak >= 2:
                        break
                else:
                    empty_streak = 0
                    last_len = len(clean)

            # Stop if idle prompt — command/pager finished.
            segment = await terminal_session.current_segment()
            if segment.is_empty_prompt:
                break

        return "\n".join(accumulated_parts), actual

    # ------------------------------------------------------------------
    # action implementations
    # ------------------------------------------------------------------

    async def _do_write(self, params: WriteParams, *, repeat: int = 1) -> str:
        terminal_session, running, _finished = await self._resolve_terminal()
        if running is None:
            return _build_process_xml(
                _A.WRITE.value, "[Error] No running process session found for default terminal"
            )

        guard_result = await check_process_writable(terminal_session, config=self._config)
        if guard_result is not None:
            return self._format_write_rejected(guard_result, terminal_name=terminal_session.name)

        # In raw mode (pager, password prompt, TUI), the process only
        # recognises \r as the Enter key; \n is ignored.  See ENTER_KEY.
        ending = ENTER_KEY if params.submit else ""
        payload = params.data + ending
        raw_output, actual = await self._batch_write_with_early_stop(
            terminal_session,
            payload,
            repeat,
        )

        drained = await _drain_terminal_after_action(
            terminal_session,
            self._registry,
            running.id,
            self._config,
        )
        output = (raw_output + drained) if drained else raw_output
        output = output or "(no output)"

        return _build_process_xml(
            _A.WRITE.value,
            output,
            terminal_name=terminal_session.name,
            session_id=running.id,
            bytes_written=len(params.data) * actual,
        )

    async def _do_submit(self, *, repeat: int = 1) -> str:
        terminal_session, running, _finished = await self._resolve_terminal()
        if running is None:
            return _build_process_xml(
                _A.SUBMIT.value, "[Error] No running process session found for default terminal"
            )

        ending = ENTER_KEY
        raw_output, actual = await self._batch_write_with_early_stop(
            terminal_session,
            ending,
            repeat,
        )

        drained = await _drain_terminal_after_action(
            terminal_session,
            self._registry,
            running.id,
            self._config,
        )
        output = (raw_output + drained) if drained else raw_output
        output = output or "(no output)"

        return _build_process_xml(
            _A.SUBMIT.value,
            output,
            terminal_name=terminal_session.name,
            session_id=running.id,
        )

    async def _do_send_keys(self, params: SendKeysParams) -> str:
        terminal_session, running, _finished = await self._resolve_terminal()
        if running is None:
            return _build_process_xml(
                _A.SEND_KEYS.value, "[Error] No running process session found for default terminal"
            )

        cursor_mode = running.cursor_key_mode

        if params.keys and needs_cursor_mode(params.keys) and cursor_mode == CursorKeyMode.UNKNOWN:
            return _build_process_xml(
                _A.SEND_KEYS.value,
                f"Session {running.id} cursor key mode is not known yet. "
                "Poll or log until startup output appears, then retry send_keys.",
                terminal_name=terminal_session.name,
                session_id=running.id,
            )

        parts: list[bytes] = []
        warnings: list[str] = []

        if params.literal:
            parts.append(params.literal.encode("utf-8"))

        for token in params.hex_bytes or []:
            try:
                parts.append(bytes([int(token, 16)]))
            except ValueError:
                warnings.append(f"Invalid hex byte: {token}")

        if params.keys:
            parts.append(encode_key_sequence(params.keys, cursor_mode))

        combined = b"".join(parts)
        if not combined:
            return _build_process_xml(_A.SEND_KEYS.value, "[Error] No key data provided.")

        await terminal_session.write(combined.decode("utf-8", errors="surrogateescape"))

        # Drain output after send_keys
        output = await _drain_terminal_after_action(
            terminal_session,
            self._registry,
            running.id,
            self._config,
        )

        return _build_process_xml(
            _A.SEND_KEYS.value,
            output or "(no output)",
            terminal_name=terminal_session.name,
            session_id=running.id,
        )

    async def _do_paste(self, params: PasteParams) -> str:
        terminal_session, running, _finished = await self._resolve_terminal()
        if running is None:
            return _build_process_xml(
                _A.PASTE.value, "[Error] No running process session found for default terminal"
            )

        payload = encode_paste(params.text, bracketed=terminal_session.bracketed_paste_enabled)
        await terminal_session.write(payload.decode("utf-8", errors="surrogateescape"))

        output = await _drain_terminal_after_action(
            terminal_session,
            self._registry,
            running.id,
            self._config,
        )
        return _build_process_xml(
            _A.PASTE.value,
            output or "(no output)",
            terminal_name=terminal_session.name,
            session_id=running.id,
        )

    async def _do_interrupt(self) -> str:
        terminal_session, running, _finished = await self._resolve_terminal()
        if running is None:
            return _build_process_xml(
                _A.INTERRUPT.value, "[Error] No running process session found for default terminal"
            )

        await terminal_session.interrupt()
        # Give the signal time to propagate through the PTY.
        await asyncio.sleep(0.5)
        # Push fresh data into the sliding buffer, then extract from it.
        # This is the same path terminal current uses — reads the full
        # buffer snapshot, not incremental PTY chunks — so it reliably
        # captures ^C, ERROR, and the new prompt even if they arrived
        # during the sleep window.
        await terminal_session.refresh_output(timeout=1.0)
        output = await terminal_session.last_command_output()
        if not output:
            segment = await terminal_session.current_segment()
            output = segment.cursor_line or "(no output)"

        # If the prompt returned, the interrupt succeeded — mark process as killed.
        segment = await terminal_session.current_segment()
        if segment.is_empty_prompt:
            self._registry.mark_exited(
                running.id,
                exit_code=None,
                exit_signal="SIGINT",
                status=ProcessStatus.KILLED,
            )

        return _build_process_xml(
            _A.INTERRUPT.value,
            output,
            terminal_name=terminal_session.name,
            session_id=running.id,
        )

    async def _do_kill(self) -> str:
        terminal_session, running, _finished = await self._resolve_terminal()
        if running is None:
            return _build_process_xml(
                _A.KILL.value, "[Error] No running process session found for default terminal"
            )

        await terminal_session.terminate()
        self._registry.mark_exited(
            running.id,
            exit_code=None,
            exit_signal="KILLED",
            status=ProcessStatus.KILLED,
        )
        return _build_process_xml(
            _A.KILL.value,
            f"Killed session {running.id} — backend terminated, terminal closed.",
            terminal_name=terminal_session.name,
            session_id=running.id,
        )

    async def _do_clear(self) -> str:
        _terminal, _running, finished = await self._resolve_terminal()
        if finished is None:
            return _build_process_xml(
                _A.CLEAR.value, "[Error] No finished process session found for default terminal"
            )
        self._registry.delete(finished.id)
        return _build_process_xml(
            _A.CLEAR.value,
            f"Cleared finished session {finished.id}.",
            terminal_name=_terminal.name,
        )

    async def _do_remove(self) -> str:
        terminal_session, running, finished = await self._resolve_terminal()

        if running is not None:
            await terminal_session.terminate()
            self._registry.mark_exited(
                running.id,
                exit_code=None,
                exit_signal="KILLED",
                status=ProcessStatus.KILLED,
            )
            self._registry.delete(running.id)
            return _build_process_xml(
                _A.REMOVE.value,
                f"Killed and removed session {running.id}.",
                terminal_name=terminal_session.name,
            )

        if finished is not None:
            self._registry.delete(finished.id)
            return _build_process_xml(
                _A.REMOVE.value,
                f"Removed finished session {finished.id}.",
                terminal_name=terminal_session.name,
            )

        return _build_process_xml(
            _A.REMOVE.value, "[Error] No process session found for default terminal"
        )

    # ------------------------------------------------------------------
    # rejected response
    # ------------------------------------------------------------------

    def _format_write_rejected(
        self,
        guard_result: TerminalGuardResult,
        *,
        terminal_name: str | None = None,
    ) -> str:

        snap = guard_result.snapshot
        parts = [
            "<process_result>",
            f"<action>{_A.WRITE.value}</action>",
            "<status>rejected</status>",
            f"<message>{xml_text(guard_result.message)}</message>",
        ]
        if terminal_name:
            parts.append(f"<terminal>{xml_text(terminal_name)}</terminal>")
        parts.extend(
            [
                "<diagnostic>",
                f"<status>{snap.status.value}</status>",
                f"<idle_ms>{snap.idle_ms}</idle_ms>",
            ]
        )
        if snap.cursor_line:
            parts.append(f"<cursor>{xml_text(snap.cursor_line)}</cursor>")
        if snap.suggestion:
            parts.append(f"<suggestion>{xml_text(snap.suggestion)}</suggestion>")
        parts.append("</diagnostic>")
        parts.append("</process_result>")
        return "\n".join(parts)
