from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any
from xml.sax.saxutils import escape as xml_escape

from framework.core.tool_manager import Tool
from framework.tools.terminal.config import TerminalRuntimeConfig
from framework.tools.terminal.managers import TerminalManagerBase
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


@dataclass(frozen=True)
class WriteParams:
    data: str = ""
    submit: bool = False


_DEFAULT_LOG_TAIL_LINES = 200
_WRITE_READ_TIMEOUT_S = 3.0  # max wait for terminal output after write/submit


async def _drain_terminal_after_action(
    terminal_session: TerminalSession,
    registry: ProcessRegistry,
    session_id: str,
    config: TerminalRuntimeConfig,
) -> str:
    """Read terminal output after write/submit. Reuses CommandTool timing:
    yield_ms as soft deadline, default_command_timeout as hard timeout,
    prompt detection for early completion.
    """
    from framework.tools.terminal.prompt import sanitize_terminal_output
    import asyncio as _asyncio
    import time as _time

    yield_window_ms = config.default_yield_ms
    hard_timeout_s = config.default_command_timeout_seconds
    start = _time.monotonic()

    output_parts: list[str] = []
    output_received = False
    prompt_stable_since: float | None = None

    while True:
        elapsed_ms = int((_time.monotonic() - start) * 1000)

        read = await terminal_session.poll_once(timeout=0.05)
        if read.stdout:
            registry.append_output(session_id, "stdout", read.stdout)
            output_parts.append(read.stdout)
            output_received = True
            prompt_stable_since = None
        if read.stderr:
            registry.append_output(session_id, "stderr", read.stderr)
            output_parts.append(read.stderr)

        # Hard timeout
        if elapsed_ms >= hard_timeout_s * 1000:
            break

        # Prompt detection (same as CommandTool)
        if output_received:
            segment = await terminal_session.current_segment()
            if segment.is_empty_prompt:
                if prompt_stable_since is None:
                    prompt_stable_since = _time.monotonic()
                elif (_time.monotonic() - prompt_stable_since) * 1000 >= config.prompt_stabilize_ms:
                    break
            else:
                prompt_stable_since = None

        # Yield window
        if elapsed_ms >= yield_window_ms:
            break

        await _asyncio.sleep(0.05)

    if output_parts:
        return sanitize_terminal_output("".join(output_parts)).rstrip()
    return ""


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


def _build_process_xml(
    action: str,
    output: str,
    *,
    session_id: str | None = None,
    status: str | None = None,
    idle_ms: int | None = None,
    bytes_written: int | None = None,
    sessions_xml: str | None = None,
) -> str:
    parts = [
        "<process_result>",
        f"<action>{action}</action>",
        f"<output>{xml_escape(output)}</output>",
    ]
    if session_id is not None:
        parts.append(f"<session_id>{session_id}</session_id>")
    if status is not None:
        parts.append(f"<status>{status}</status>")
    if idle_ms is not None:
        parts.append(f"<idle_ms>{idle_ms}</idle_ms>")
    if bytes_written is not None:
        parts.append(f"<bytes_written>{bytes_written}</bytes_written>")
    if sessions_xml is not None:
        parts.append(f"<sessions>{sessions_xml}</sessions>")
    parts.append("</process_result>")
    return "\n".join(parts)


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
            "Interact with a running command in the default terminal. Actions:\n"
            "  log       -- read full output history (optional: offset, limit for paging)\n"
            "  list      -- list all running and recently finished sessions\n"
            "  write     -- send text to the command's stdin\n"
            "  submit    -- send Enter key to stdin (confirm a prompt after write)\n"
            "  send_keys -- send key sequences: arrows, c-c (Ctrl+C), escape, tab, f1-f12, etc.\n"
            "  paste     -- paste multi-line text\n"
            "  interrupt -- send Ctrl+C to stop the command\n"
            "  kill      -- forcefully terminate the command\n"
            "  clear     -- remove a finished session record\n"
            "  remove    -- kill (if running) and remove the session\n\n"
            "IMPORTANT: NEVER write a password without asking the user first. "
            "If a command prompts for a password, STOP and ask the user. "
            "Only use write for passwords after the user explicitly provides one.\n"
            "After providing input, use 'terminal current' to check the screen.\n"
            "To answer a prompt: process write data=\"USER_PROVIDED_VALUE\" submit=true.\n"
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
                    "description": "poll | log | list | write | submit | send_keys | paste | interrupt | kill | clear | remove",
                },
                "data": {
                    "type": "string",
                    "description": "Text to send to stdin (write action). Include \\n for newline if needed.",
                },
                "keys": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Key tokens: arrows, c-c (Ctrl+C), escape, enter, tab, backspace, f1-f12, hex:NN",
                },
                "hex": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Hex bytes for send_keys, e.g. [\"1b\", \"0d\"]",
                },
                "literal": {
                    "type": "string",
                    "description": "Literal text to send with send_keys",
                },
                "text": {
                    "type": "string",
                    "description": "Multi-line text to paste (paste action)",
                },
                "submit": {
                    "type": "boolean",
                    "description": "Send Enter key after writing (write action). Use for passwords, y/n confirmations.",
                },
                "offset": {
                    "type": "integer",
                    "description": "Skip first N lines (log action)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max lines to return (log action)",
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

        match action:
            case ProcessAction.LIST:
                return await self._do_list()
            # POLL removed (see pty_keys.py comment)
            # case ProcessAction.POLL:
            #     return await self._do_poll()
            case ProcessAction.LOG:
                return await self._do_log(
                    offset=int(kwargs.get("offset") or 0),
                    limit=int(kwargs.get("limit") or _DEFAULT_LOG_TAIL_LINES),
                )
            case ProcessAction.WRITE:
                return await self._do_write(
                    WriteParams(
                        data=kwargs.get("data", ""),
                        submit=kwargs.get("submit", False),
                    ),
                )
            case ProcessAction.SUBMIT:
                return await self._do_submit()
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
                    PasteParams(
                        text=kwargs.get("text", ""),
                    ),
                )
            case ProcessAction.INTERRUPT:
                return await self._do_interrupt()
            case ProcessAction.KILL:
                return await self._do_kill()
            case ProcessAction.CLEAR:
                return await self._do_clear()
            case ProcessAction.REMOVE:
                return await self._do_remove()

    async def _resolve_terminal(self) -> tuple[TerminalSession, ProcessSession | None, ProcessSession | None]:
        """Resolve the default terminal and its running/finished process sessions.

        Returns (terminal_session, running_process, finished_process).
        The running/finished entries may be None.
        """
        terminal_session = await self._manager.get_default()
        if terminal_session is None:
            raise ValueError("No default terminal session available")
        name = terminal_session.name
        running = self._registry.get_running_by_terminal(name)
        finished = self._registry.get_finished_by_terminal(name)
        return terminal_session, running, finished

    async def _do_list(self) -> str:
        running = self._registry.list_running()
        finished = self._registry.list_finished()
        if not running and not finished:
            return _build_process_xml("list", "No running or recent sessions.")

        lines: list[str] = []
        session_entries: list[str] = []
        for s in running:
            runtime = self._registry.running_runtime(s.id)
            lines.append(_format_list_line(s, runtime))
            idle = runtime.idle_ms if runtime else 0
            session_entries.append(
                f'<session id="{s.id}" status="running" '
                f'command="{xml_escape(s.command)}" '
                f'elapsed_ms="{int(((s.ended_at or time.time()) - s.started_at) * 1000)}"'
                f'idle_ms="{idle}" />'
            )
        for s in finished:
            lines.append(_format_list_line(s))
            session_entries.append(
                f'<session id="{s.id}" status="{s.status.value}" '
                f'command="{xml_escape(s.command)}" '
                f'elapsed_ms="{int(((s.ended_at or 0) - s.started_at) * 1000)}" '
                f'exit_code="{s.exit_code}" />'
            )

        return _build_process_xml(
            "list", "\n".join(lines),
            sessions_xml="\n".join(session_entries),
        )

    # _do_poll removed — poll drains pending output but cannot detect command
    # completion reliably in PTY mode. After write+submit, use `terminal current`
    # to see the terminal screen state instead of polling for new output.
    # See pty_keys.py ProcessAction comment for details.

    async def _do_log(
        self, offset: int = 0, limit: int = _DEFAULT_LOG_TAIL_LINES
    ) -> str:
        _terminal, running, finished = await self._resolve_terminal()

        session = running or finished
        if session is None:
            return _build_process_xml("log", "[Error] No process session found for default terminal")

        all_lines = session.aggregated.splitlines()
        total = len(all_lines)
        using_default_tail = offset == 0 and limit == _DEFAULT_LOG_TAIL_LINES
        sliced = all_lines[offset : offset + limit]
        output = "\n".join(sliced) or "(no output yet)"

        tail_note = ""
        if using_default_tail and total > _DEFAULT_LOG_TAIL_LINES:
            tail_note = f"\n\n[showing last {_DEFAULT_LOG_TAIL_LINES} of {total} lines; pass offset/limit to page]"

        runtime = (
            self._registry.running_runtime(session.id)
            if session.status == ProcessStatus.RUNNING
            else None
        )
        hint = _build_input_wait_hint(runtime)

        return _build_process_xml(
            "log", output + tail_note + hint,
            session_id=session.id,
            status=session.status.value,
            idle_ms=runtime.idle_ms if runtime else None,
        )

    async def _do_write(self, params: WriteParams) -> str:
        terminal_session, running, _finished = await self._resolve_terminal()
        if running is None:
            return _build_process_xml("write", "[Error] No running process session found for default terminal")

        await terminal_session.write(params.data)
        if params.submit:
            await terminal_session.write("\r")

        info = f"Wrote {len(params.data)} bytes to session {running.id}"
        if params.submit:
            info += " + Enter"

        output = await _drain_terminal_after_action(terminal_session, self._registry, running.id, self._config)
        full_output = f"{info}.\nTerminal output:\n{output}" if output else f"{info}."

        return _build_process_xml(
            "write", full_output,
            session_id=running.id,
            bytes_written=len(params.data),
        )

    async def _do_submit(self) -> str:
        terminal_session, running, _finished = await self._resolve_terminal()
        if running is None:
            return _build_process_xml("submit", "[Error] No running process session found for default terminal")

        await terminal_session.write("\r")
        output = await _drain_terminal_after_action(terminal_session, self._registry, running.id, self._config)
        full_output = f"Sent Enter to session {running.id}.\nTerminal output:\n{output}" if output else f"Sent Enter to session {running.id}."
        return _build_process_xml("submit", full_output, session_id=running.id)

    async def _do_send_keys(self, params: SendKeysParams) -> str:
        terminal_session, running, _finished = await self._resolve_terminal()
        if running is None:
            return _build_process_xml("send_keys", "[Error] No running process session found for default terminal")

        cursor_mode = running.cursor_key_mode

        if params.keys and needs_cursor_mode(params.keys) and cursor_mode == CursorKeyMode.UNKNOWN:
            return _build_process_xml(
                "send_keys",
                f"Session {running.id} cursor key mode is not known yet. "
                "Poll or log until startup output appears, then retry send_keys.",
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
            return _build_process_xml("send_keys", "[Error] No key data provided.")

        await terminal_session.write(combined.decode("utf-8", errors="surrogateescape"))

        result_text = f"Sent {len(combined)} bytes to session {running.id}."
        if warnings:
            result_text += "\nWarnings:\n- " + "\n- ".join(warnings)
        return _build_process_xml("send_keys", result_text, session_id=running.id)

    async def _do_paste(self, params: PasteParams) -> str:
        terminal_session, running, _finished = await self._resolve_terminal()
        if running is None:
            return _build_process_xml("paste", "[Error] No running process session found for default terminal")

        payload = encode_paste(params.text, bracketed=terminal_session.bracketed_paste_enabled)
        await terminal_session.write(payload.decode("utf-8", errors="surrogateescape"))
        return _build_process_xml("paste", f"Pasted {len(params.text)} chars to session {running.id}.", session_id=running.id)

    async def _do_interrupt(self) -> str:
        terminal_session, running, _finished = await self._resolve_terminal()
        if running is None:
            return _build_process_xml("interrupt", "[Error] No running process session found for default terminal")

        await terminal_session.interrupt()
        return _build_process_xml("interrupt", f"Sent interrupt (Ctrl+C) to session {running.id}.", session_id=running.id)

    async def _do_kill(self) -> str:
        terminal_session, running, _finished = await self._resolve_terminal()
        if running is None:
            return _build_process_xml("kill", "[Error] No running process session found for default terminal")

        await terminal_session.terminate()
        self._registry.mark_exited(
            running.id,
            exit_code=None,
            exit_signal="KILLED",
            status=ProcessStatus.KILLED,
        )
        return _build_process_xml("kill", f"Killed session {running.id}.", session_id=running.id)

    async def _do_clear(self) -> str:
        _terminal, _running, finished = await self._resolve_terminal()
        if finished is None:
            return _build_process_xml("clear", "[Error] No finished process session found for default terminal")
        self._registry.delete(finished.id)
        return _build_process_xml("clear", f"Cleared finished session {finished.id}.")

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
            return _build_process_xml("remove", f"Killed and removed session {running.id}.")

        if finished is not None:
            self._registry.delete(finished.id)
            return _build_process_xml("remove", f"Removed finished session {finished.id}.")

        return _build_process_xml("remove", "[Error] No process session found for default terminal")
