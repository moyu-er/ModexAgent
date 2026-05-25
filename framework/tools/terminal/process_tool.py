"""ProcessTool — follow-up actions for running or recently finished processes.

Manages already-started command sessions via the ProcessRegistry.
Does NOT start commands (that's CommandTool's job).
"""

from __future__ import annotations

from typing import Any

from framework.core.tool_manager import Tool
from framework.tools.terminal.process_registry import ProcessRegistry
from framework.tools.terminal.types import ProcessStatus

KEY_BYTES: dict[str, str] = {
    "enter": "\r",
    "escape": "\x1b",
    "ctrl+c": "\x03",
    "ctrl+d": "\x04",
    "tab": "\t",
    "backspace": "\x7f",
    "up": "\x1b[A",
    "down": "\x1b[B",
    "right": "\x1b[C",
    "left": "\x1b[D",
    "home": "\x1b[H",
    "end": "\x1b[F",
    "pageup": "\x1b[5~",
    "pagedown": "\x1b[6~",
}


class ProcessTool(Tool):
    """Follow-up actions for running or recently finished process sessions."""

    def __init__(
        self,
        registry: ProcessRegistry,
        manager: Any,
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
            "Manage running or recently finished process sessions. "
            "Actions: list, poll, log, write, submit, send_keys, paste, "
            "interrupt, kill, clear, remove."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "list", "poll", "log", "write", "submit",
                        "send_keys", "paste", "interrupt", "kill",
                        "clear", "remove",
                    ],
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
                    "description": "Named keys to send (for send_keys action).",
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
                    "description": "Use bracketed paste mode (for paste action).",
                },
                "eof": {
                    "type": "boolean",
                    "description": "Signal end of input after writing (for write action).",
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
                    "description": "Poll wait timeout in milliseconds.",
                },
            },
            "required": ["action"],
        }

    async def execute(self, **kwargs: Any) -> str:
        action = kwargs.get("action", "")
        handler = {
            "list": self._action_list,
            "poll": self._action_poll,
            "log": self._action_log,
            "write": self._action_write,
            "submit": self._action_submit,
            "send_keys": self._action_send_keys,
            "paste": self._action_paste,
            "interrupt": self._action_interrupt,
            "kill": self._action_kill,
            "clear": self._action_clear,
            "remove": self._action_remove,
        }.get(action)
        if handler is None:
            return f"[Error] Unknown action: {action}"
        return await handler(**kwargs)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    async def _action_list(self, **_kwargs: Any) -> str:
        running = self._registry.list_running()
        finished = self._registry.list_finished()
        lines: list[str] = ["[Process List]"]
        if running:
            lines.append("")
            lines.append("Running:")
            for s in running:
                lines.append(f"  {s.id}  {s.command}  (pid={s.pid})")
        if finished:
            lines.append("")
            lines.append("Finished:")
            for s in finished:
                lines.append(f"  {s.id}  {s.command}  status={s.status.value}")
        if not running and not finished:
            lines.append("")
            lines.append("No process sessions.")
        return "\n".join(lines)

    async def _action_poll(self, **kwargs: Any) -> str:
        session_id = kwargs.get("session_id", "")
        session = self._registry.get_running(session_id)
        if session is None:
            session = self._registry.get_finished(session_id)
        if session is None:
            return f"[Error] Session not found: {session_id}"

        status_value = session.status.value

        # Drain pending output
        pending = self._registry.drain_pending(session_id)
        has_output = bool(pending.stdout or pending.stderr)

        lines: list[str] = [
            "[Process Poll]",
            f"session_id: {session_id}",
            f"status: {status_value}",
        ]

        if has_output:
            lines.extend(["", "[Pending Output]", (pending.stdout + pending.stderr).rstrip()])
        else:
            lines.extend(["", "[Pending Output]", "(no new output)"])

        runtime = self._registry.running_runtime(session_id)
        if runtime is not None:
            lines.extend(["", "[State]"])
            lines.append(f"stdin_writable: {str(runtime.stdin_writable).lower()}")
            lines.append(f"waiting_for_input: {str(runtime.waiting_for_input).lower()}")
            lines.append(f"idle_ms: {runtime.idle_ms}")
            velocity_str = "active" if runtime.output_velocity.is_active else "inactive"
            lines.append(f"output_velocity: {velocity_str}")
            if runtime.waiting_for_input:
                lines.append(
                    "hint: Process appears to be waiting for input. Use process write/submit to respond."
                )
            elif runtime.output_velocity.is_active:
                lines.append("hint: Output is still being produced. Poll again in a few seconds.")

        return "\n".join(lines)

    async def _action_log(self, **kwargs: Any) -> str:
        session_id = kwargs.get("session_id", "")
        session = self._registry.get_running(session_id) or self._registry.get_finished(session_id)
        if session is None:
            return f"[Error] Session not found: {session_id}"

        offset = int(kwargs.get("offset") or 0)
        limit = int(kwargs.get("limit") or 50)

        all_lines = session.aggregated.splitlines()
        total = len(all_lines)
        sliced = all_lines[offset : offset + limit]
        char_count = sum(len(line) for line in sliced)

        lines: list[str] = [
            "[Process Log]",
            f"session_id: {session_id}",
            f"status: {session.status.value}",
            f"lines: {offset + 1}-{offset + len(sliced)} of {total}" if total > 0 else f"lines: 0 of 0",
            f"chars: {char_count}",
        ]
        if sliced:
            lines.extend(["", "[Output]"])
            lines.extend(sliced)
        return "\n".join(lines)

    async def _action_write(self, **kwargs: Any) -> str:
        session_id = kwargs.get("session_id", "")
        session = self._registry.get_running(session_id)
        if session is None:
            return f"[Error] Session not found or not running: {session_id}"
        data = kwargs.get("data", "")
        terminal = await self._manager.get_or_create(session.terminal)
        await terminal.write(data)
        return f"[Process Write]\nsession_id: {session_id}\nbytes_written: {len(data)}"

    async def _action_submit(self, **kwargs: Any) -> str:
        session_id = kwargs.get("session_id", "")
        session = self._registry.get_running(session_id)
        if session is None:
            return f"[Error] Session not found or not running: {session_id}"
        terminal = await self._manager.get_or_create(session.terminal)
        await terminal.write("\r")
        return f"[Process Submit]\nsession_id: {session_id}"

    async def _action_send_keys(self, **kwargs: Any) -> str:
        session_id = kwargs.get("session_id", "")
        session = self._registry.get_running(session_id)
        if session is None:
            return f"[Error] Session not found or not running: {session_id}"

        parts: list[str] = []

        # Named keys
        for key in kwargs.get("keys") or []:
            mapped = KEY_BYTES.get(key)
            if mapped is None:
                return f"[Error] Unknown key: {key}"
            parts.append(mapped)

        # Hex bytes
        for token in kwargs.get("hex") or []:
            parts.append(chr(int(token, 16)))

        # Literal string
        literal = kwargs.get("literal")
        if literal:
            parts.append(literal)

        combined = "".join(parts)
        terminal = await self._manager.get_or_create(session.terminal)
        await terminal.write(combined)
        return f"[Process Send Keys]\nsession_id: {session_id}\nbytes_sent: {len(combined)}"

    async def _action_paste(self, **kwargs: Any) -> str:
        session_id = kwargs.get("session_id", "")
        session = self._registry.get_running(session_id)
        if session is None:
            return f"[Error] Session not found or not running: {session_id}"

        text = kwargs.get("text", "")
        bracketed = kwargs.get("bracketed", False)

        if bracketed:
            payload = f"\x1b[200~{text}\x1b[201~"
        else:
            payload = text

        terminal = await self._manager.get_or_create(session.terminal)
        await terminal.write(payload)
        return f"[Process Paste]\nsession_id: {session_id}\nchars: {len(text)}"

    async def _action_interrupt(self, **kwargs: Any) -> str:
        session_id = kwargs.get("session_id", "")
        session = self._registry.get_running(session_id)
        if session is None:
            return f"[Error] Session not found or not running: {session_id}"
        terminal = await self._manager.get_or_create(session.terminal)
        await terminal.interrupt()
        return f"[Process Interrupt]\nsession_id: {session_id}"

    async def _action_kill(self, **kwargs: Any) -> str:
        session_id = kwargs.get("session_id", "")
        session = self._registry.get_running(session_id)
        if session is None:
            return f"[Error] Session not found or not running: {session_id}"
        terminal = await self._manager.get_or_create(session.terminal)
        await terminal.terminate()
        self._registry.mark_exited(
            session_id,
            exit_code=None,
            exit_signal="KILLED",
            status=ProcessStatus.KILLED,
        )
        return f"[Process Kill]\nsession_id: {session_id}\nstatus: killed"

    async def _action_clear(self, **kwargs: Any) -> str:
        session_id = kwargs.get("session_id", "")
        session = self._registry.get_finished(session_id)
        if session is None:
            return f"[Error] Session not found or not finished: {session_id}"
        self._registry.delete(session_id)
        return f"[Process Clear]\nsession_id: {session_id}\nremoved: true"

    async def _action_remove(self, **kwargs: Any) -> str:
        session_id = kwargs.get("session_id", "")
        running = self._registry.get_running(session_id)
        if running is not None:
            terminal = await self._manager.get_or_create(running.terminal)
            await terminal.terminate()
            self._registry.mark_exited(
                session_id,
                exit_code=None,
                exit_signal="KILLED",
                status=ProcessStatus.KILLED,
            )
            self._registry.delete(session_id)
            return f"[Process Remove]\nsession_id: {session_id}\nkilled_and_removed: true"
        finished = self._registry.get_finished(session_id)
        if finished is not None:
            self._registry.delete(session_id)
            return f"[Process Remove]\nsession_id: {session_id}\nremoved: true"
        return f"[Error] Session not found: {session_id}"
