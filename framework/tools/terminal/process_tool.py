from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from framework.core.tool_manager import Tool
from framework.tools.terminal.manager import TerminalManager
from framework.tools.terminal.process_registry import (
    ProcessRegistry,
    ProcessSession,
    RunningSessionRuntime,
)
from framework.tools.terminal.pty_keys import (
    CursorKeyMode,
    ProcessAction,
    encode_key_sequence,
    encode_paste,
    needs_cursor_mode,
)
from framework.tools.terminal.session import TerminalSession
from framework.tools.terminal.types import ProcessStatus


@dataclass(frozen=True)
class SendKeysParams:
    keys: list[str] | None = None
    hex_bytes: list[str] | None = None
    literal: str | None = None


@dataclass(frozen=True)
class PasteParams:
    text: str = ""
    bracketed: bool = False


@dataclass(frozen=True)
class WriteParams:
    data: str = ""
    eof: bool = False


_DEFAULT_LOG_TAIL_LINES = 200


def _format_duration(ms: int) -> str:
    if ms < 1000:
        return f"{ms}ms"
    seconds = ms // 1000
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    secs = seconds % 60
    return f"{minutes}m{secs:02d}s"


def _format_list_line(session: ProcessSession, runtime: RunningSessionRuntime | None = None) -> str:
    elapsed_ms = int((session.ended_at or 0) - session.started_at) if session.ended_at else 0
    if session.status != ProcessStatus.RUNNING:
        exit_part = f"exit={session.exit_code}" if session.exit_code is not None else ""
        signal_part = f"signal={session.exit_signal}" if session.exit_signal is not None else ""
        suffix = " ".join(p for p in (exit_part, signal_part) if p)
        return f"{session.id}  {session.status.value:9s}  {_format_duration(elapsed_ms)}  {session.command}  ({suffix})"

    wait_marker = " [input-wait]" if runtime and runtime.waiting_for_input else ""
    idle_str = f" idle={_format_duration(runtime.idle_ms)}" if runtime else ""
    return f"{session.id}  running   {_format_duration(elapsed_ms)}{idle_str}{wait_marker}  ::  {session.command}"


def _build_input_wait_hint(runtime: RunningSessionRuntime | None) -> str:
    if not runtime or not runtime.waiting_for_input:
        return ""
    idle = _format_duration(runtime.idle_ms)
    return (
        f"\n\nNo new output for {idle}; this session may be waiting for input. "
        "Use process write, send_keys, submit, or paste to provide input."
    )


def _build_output_velocity_hint(runtime: RunningSessionRuntime | None) -> str:
    if not runtime or not runtime.output_velocity.is_active:
        return ""
    return "\n\nOutput is still being produced. Poll again in a few seconds."


class ProcessTool(Tool):
    """Manage running exec sessions for commands already started.

    Use poll/log when you need status, logs, quiet-success confirmation, or
    completion confirmation.  Use poll/log also for input-wait hints.
    Use write/send_keys/submit/paste/kill for input or intervention.

    Actions:
      list       — list all running and finished sessions
      poll       — drain pending output; shows input-wait hints when idle
      log        — read aggregated output with line paging
      write      — write raw data to session stdin
      submit     — send CR (Enter) to session stdin
      send_keys  — send encoded key sequences (named keys, modifiers, hex)
      paste      — paste text with optional bracketed-paste wrapping
      interrupt  — send interrupt signal (Ctrl+C equivalent)
      kill       — terminate the process and mark as killed
      clear      — remove a finished session from the registry
      remove     — kill (if running) and remove a session
    """

    def __init__(
        self,
        registry: ProcessRegistry,
        manager: TerminalManager,
    ) -> None:
        super().__init__()
        self._registry = registry
        self._manager = manager

    @property
    def name(self) -> str:
        return "process"

    @property
    def description(self) -> str:
        return (
            "Manage running exec sessions for commands already started: "
            "list, poll, log, write, send_keys, submit, paste, interrupt, kill, clear, remove. "
            "Use poll/log when you need status, logs, quiet-success confirmation, or input-wait hints. "
            "Use write/send_keys/submit/paste/kill for input or intervention."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [a.value for a in ProcessAction],
                    "description": "Action to perform on the process session.",
                },
                "session_id": {
                    "type": "string",
                    "description": "Process session ID (not required for list).",
                },
                "data": {
                    "type": "string",
                    "description": "Raw data to write (for write action).",
                },
                "keys": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Key tokens to send (for send_keys action). "
                        'Formats: single char ("a"), ctrl ("c-c"), alt ("m-x"), '
                        'named ("escape", "enter", "tab", "backspace", "delete"), '
                        'arrows ("up", "down", "left", "right"), '
                        'function ("f1"-"f12"), hex ("hex:1b").'
                    ),
                },
                "hex": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Hex byte strings to send (for send_keys action).",
                },
                "literal": {
                    "type": "string",
                    "description": "Literal string to send (for send_keys action).",
                },
                "text": {
                    "type": "string",
                    "description": "Text to paste (for paste action).",
                },
                "bracketed": {
                    "type": "boolean",
                    "description": "Wrap paste in bracketed-paste mode (for paste action).",
                },
                "eof": {
                    "type": "boolean",
                    "description": "Close stdin after writing (for write action).",
                },
                "offset": {
                    "type": "integer",
                    "description": "Line offset for log paging.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max lines for log paging.",
                },
                "timeout": {
                    "type": "integer",
                    "description": "For poll: wait up to this many milliseconds before returning.",
                },
            },
            "required": ["action"],
        }

    async def execute(self, **kwargs: Any) -> str:  # noqa: ANN401
        action_raw = kwargs.get("action", "")
        try:
            action = ProcessAction(action_raw)
        except ValueError:
            return f"[Error] Unknown action: {action_raw}"

        session_id: str = kwargs.get("session_id", "")

        match action:
            case ProcessAction.LIST:
                return await self._do_list()
            case ProcessAction.POLL:
                return await self._do_poll(session_id)
            case ProcessAction.LOG:
                return await self._do_log(
                    session_id,
                    offset=int(kwargs.get("offset") or 0),
                    limit=int(kwargs.get("limit") or _DEFAULT_LOG_TAIL_LINES),
                )
            case ProcessAction.WRITE:
                return await self._do_write(
                    session_id,
                    WriteParams(
                        data=kwargs.get("data", ""),
                        eof=kwargs.get("eof", False),
                    ),
                )
            case ProcessAction.SUBMIT:
                return await self._do_submit(session_id)
            case ProcessAction.SEND_KEYS:
                return await self._do_send_keys(
                    session_id,
                    SendKeysParams(
                        keys=kwargs.get("keys"),
                        hex_bytes=kwargs.get("hex"),
                        literal=kwargs.get("literal"),
                    ),
                )
            case ProcessAction.PASTE:
                return await self._do_paste(
                    session_id,
                    PasteParams(
                        text=kwargs.get("text", ""),
                        bracketed=kwargs.get("bracketed", False),
                    ),
                )
            case ProcessAction.INTERRUPT:
                return await self._do_interrupt(session_id)
            case ProcessAction.KILL:
                return await self._do_kill(session_id)
            case ProcessAction.CLEAR:
                return await self._do_clear(session_id)
            case ProcessAction.REMOVE:
                return await self._do_remove(session_id)

    async def _resolve_terminal(self, terminal_name: str) -> TerminalSession:
        return await self._manager.get_or_create(terminal_name)

    def _require_running(self, session_id: str) -> ProcessSession | str:
        session = self._registry.get_running(session_id)
        if session is None:
            return f"[Error] No active session found for {session_id}"
        return session

    async def _do_list(self) -> str:
        running = self._registry.list_running()
        finished = self._registry.list_finished()
        if not running and not finished:
            return "No running or recent sessions."

        lines: list[str] = []
        for s in running:
            runtime = self._registry.running_runtime(s.id)
            lines.append(_format_list_line(s, runtime))
        for s in finished:
            lines.append(_format_list_line(s))
        return "\n".join(lines)

    async def _do_poll(self, session_id: str) -> str:
        session = self._registry.get_running(session_id)
        if session is not None:
            pending = self._registry.drain_pending(session_id)
            output = (pending.stdout + pending.stderr).rstrip() or "(no new output)"
            runtime = self._registry.running_runtime(session_id)
            hint = _build_input_wait_hint(runtime) or _build_output_velocity_hint(runtime)
            if not hint:
                hint = "\n\nProcess still running."

            # Screen snapshot for TUI programs
            screen_section = ""
            terminal = await self._resolve_terminal(session.terminal)
            if terminal.cursor_key_mode == CursorKeyMode.APPLICATION:
                segment = await terminal.current_segment()
                if segment and segment.text.strip():
                    screen_section = (
                        f"\n\n[Screen]\n{segment.text.rstrip()}"
                        "\nhint: TUI program detected. Use send_keys for interaction."
                    )

            return output + hint + screen_section

        finished = self._registry.get_finished(session_id)
        if finished is not None:
            tail = finished.tail or "(no output recorded)"
            exit_info = (
                f"signal {finished.exit_signal}"
                if finished.exit_signal is not None
                else f"code {finished.exit_code or 0}"
            )
            return f"{tail}\n\nProcess exited with {exit_info}."

        return f"[Error] No session found for {session_id}"

    async def _do_log(
        self, session_id: str, offset: int = 0, limit: int = _DEFAULT_LOG_TAIL_LINES
    ) -> str:
        session = self._registry.get_running(session_id) or self._registry.get_finished(session_id)
        if session is None:
            return f"[Error] No session found for {session_id}"

        all_lines = session.aggregated.splitlines()
        total = len(all_lines)
        using_default_tail = offset == 0 and limit == _DEFAULT_LOG_TAIL_LINES
        sliced = all_lines[offset : offset + limit]
        output = "\n".join(sliced) or "(no output yet)"

        tail_note = ""
        if using_default_tail and total > _DEFAULT_LOG_TAIL_LINES:
            tail_note = f"\n\n[showing last {_DEFAULT_LOG_TAIL_LINES} of {total} lines; pass offset/limit to page]"

        runtime = (
            self._registry.running_runtime(session_id)
            if session.status == ProcessStatus.RUNNING
            else None
        )
        hint = _build_input_wait_hint(runtime)

        return output + tail_note + hint

    async def _do_write(self, session_id: str, params: WriteParams) -> str:
        result = self._require_running(session_id)
        if isinstance(result, str):
            return result
        session = result

        terminal = await self._resolve_terminal(session.terminal)
        await terminal.write(params.data)
        eof_note = " (stdin closed)" if params.eof else ""
        return f"Wrote {len(params.data)} bytes to session {session_id}{eof_note}."

    async def _do_submit(self, session_id: str) -> str:
        result = self._require_running(session_id)
        if isinstance(result, str):
            return result

        terminal = await self._resolve_terminal(result.terminal)
        await terminal.write("\r")
        return f"Submitted session {session_id} (sent CR)."

    async def _do_send_keys(self, session_id: str, params: SendKeysParams) -> str:
        result = self._require_running(session_id)
        if isinstance(result, str):
            return result
        session = result

        cursor_mode = session.cursor_key_mode

        if params.keys and needs_cursor_mode(params.keys) and cursor_mode == CursorKeyMode.UNKNOWN:
            return (
                f"Session {session_id} cursor key mode is not known yet. "
                "Poll or log until startup output appears, then retry send_keys."
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
            return "[Error] No key data provided."

        terminal = await self._resolve_terminal(session.terminal)
        await terminal.write(combined.decode("utf-8", errors="surrogateescape"))

        result_text = f"Sent {len(combined)} bytes to session {session_id}."
        if warnings:
            result_text += "\nWarnings:\n- " + "\n- ".join(warnings)
        return result_text

    async def _do_paste(self, session_id: str, params: PasteParams) -> str:
        result = self._require_running(session_id)
        if isinstance(result, str):
            return result

        payload = encode_paste(params.text, bracketed=params.bracketed)
        terminal = await self._resolve_terminal(result.terminal)
        await terminal.write(payload.decode("utf-8", errors="surrogateescape"))
        return f"Pasted {len(params.text)} chars to session {session_id}."

    async def _do_interrupt(self, session_id: str) -> str:
        result = self._require_running(session_id)
        if isinstance(result, str):
            return result

        terminal = await self._resolve_terminal(result.terminal)
        await terminal.interrupt()
        return f"Sent interrupt (Ctrl+C) to session {session_id}."

    async def _do_kill(self, session_id: str) -> str:
        result = self._require_running(session_id)
        if isinstance(result, str):
            return result
        session = result

        terminal = await self._resolve_terminal(session.terminal)
        await terminal.terminate()
        self._registry.mark_exited(
            session_id,
            exit_code=None,
            exit_signal="KILLED",
            status=ProcessStatus.KILLED,
        )
        return f"Killed session {session_id}."

    async def _do_clear(self, session_id: str) -> str:
        session = self._registry.get_finished(session_id)
        if session is None:
            return f"[Error] Session not found or not finished: {session_id}"
        self._registry.delete(session_id)
        return f"Cleared finished session {session_id}."

    async def _do_remove(self, session_id: str) -> str:
        running = self._registry.get_running(session_id)
        if running is not None:
            terminal = await self._resolve_terminal(running.terminal)
            await terminal.terminate()
            self._registry.mark_exited(
                session_id,
                exit_code=None,
                exit_signal="KILLED",
                status=ProcessStatus.KILLED,
            )
            self._registry.delete(session_id)
            return f"Killed and removed session {session_id}."

        finished = self._registry.get_finished(session_id)
        if finished is not None:
            self._registry.delete(session_id)
            return f"Removed finished session {session_id}."

        return f"[Error] No session found for {session_id}"
