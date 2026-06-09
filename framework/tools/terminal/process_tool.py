from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from framework.core.tool_manager import Tool
from framework.tools.terminal.config import TerminalRuntimeConfig
from framework.tools.terminal.managers import TerminalManagerBase
from framework.tools.terminal.process_registry import (
    ProcessRegistry,
    ProcessSession,
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
from framework.utils.xml import xml_text


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


_WRITE_READ_TIMEOUT_S = 3.0  # max wait for terminal output after write/submit


async def _drain_terminal_after_action(
    terminal_session: TerminalSession,
    registry: ProcessRegistry,
    session_id: str,
    config: TerminalRuntimeConfig,
) -> str:
    """Read terminal output after write/submit. Uses shared poll_until_settled:
    yield_ms as soft deadline, default_command_timeout as hard timeout,
    prompt detection for early completion.
    """
    from framework.tools.terminal.poll_loop import poll_until_settled
    from framework.tools.terminal.prompt import sanitize_terminal_output

    result = await poll_until_settled(
        terminal_session, registry, session_id, config,
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
        f"<action>{action}</action>",
        f"<output>{xml_text(output)}</output>",
    ]
    if terminal_name is not None:
        parts.append(f"<terminal>{terminal_name}</terminal>")
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
            "  write     -- send text to the command's stdin\n"
            "  submit    -- send Enter key to stdin (confirm a prompt after write)\n"
            "  send_keys -- send key sequences: arrows, c-c (Ctrl+C), escape, tab, f1-f12\n"
            "  paste     -- paste multi-line text\n"
            "  interrupt -- send Ctrl+C to stop the command\n"
            "  kill      -- forcefully terminate the command\n"
            "  clear     -- remove a finished session record\n"
            "  remove    -- kill (if running) and remove the session\n\n"
            "IMPORTANT: NEVER write a password without asking the user first. "
            "If a command prompts for a password, STOP and ask the user. "
            "Only use write for passwords after the user explicitly provides one.\n"
            "After providing input, use 'terminal current' to check the result.\n"
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
                    "description": "write | submit | send_keys | paste | interrupt | kill | clear | remove",
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
            case ProcessAction.WRITE:
                return await self._do_write(
                    WriteParams(
                        data=kwargs.get("data", ""),
                        submit=kwargs.get("submit", True),
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
            terminal_name=terminal_session.name,
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
        return _build_process_xml("submit", full_output, terminal_name=terminal_session.name, session_id=running.id)

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
            return _build_process_xml("send_keys", "[Error] No key data provided.")

        await terminal_session.write(combined.decode("utf-8", errors="surrogateescape"))

        result_text = f"Sent {len(combined)} bytes to session {running.id}."
        if warnings:
            result_text += "\nWarnings:\n- " + "\n- ".join(warnings)
        return _build_process_xml("send_keys", result_text, terminal_name=terminal_session.name, session_id=running.id)

    async def _do_paste(self, params: PasteParams) -> str:
        terminal_session, running, _finished = await self._resolve_terminal()
        if running is None:
            return _build_process_xml("paste", "[Error] No running process session found for default terminal")

        payload = encode_paste(params.text, bracketed=terminal_session.bracketed_paste_enabled)
        await terminal_session.write(payload.decode("utf-8", errors="surrogateescape"))
        return _build_process_xml("paste", f"Pasted {len(params.text)} chars to session {running.id}.", terminal_name=terminal_session.name, session_id=running.id)

    async def _do_interrupt(self) -> str:
        terminal_session, running, _finished = await self._resolve_terminal()
        if running is None:
            return _build_process_xml("interrupt", "[Error] No running process session found for default terminal")

        await terminal_session.interrupt()
        return _build_process_xml("interrupt", f"Sent interrupt (Ctrl+C) to session {running.id}.", terminal_name=terminal_session.name, session_id=running.id)

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
        return _build_process_xml("kill", f"Killed session {running.id}.", terminal_name=terminal_session.name, session_id=running.id)

    async def _do_clear(self) -> str:
        _terminal, _running, finished = await self._resolve_terminal()
        if finished is None:
            return _build_process_xml("clear", "[Error] No finished process session found for default terminal")
        self._registry.delete(finished.id)
        return _build_process_xml("clear", f"Cleared finished session {finished.id}.", terminal_name=_terminal.name)

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
            return _build_process_xml("remove", f"Killed and removed session {running.id}.", terminal_name=terminal_session.name)

        if finished is not None:
            self._registry.delete(finished.id)
            return _build_process_xml("remove", f"Removed finished session {finished.id}.", terminal_name=terminal_session.name)

        return _build_process_xml("remove", "[Error] No process session found for default terminal")
