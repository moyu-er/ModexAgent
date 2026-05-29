"""TerminalSession — single named session wrapping a TerminalBackend."""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from xml.sax.saxutils import escape as xml_escape

from framework.tools.terminal.prompt import (
    _strip_ansi_and_da1,
    is_prompt_ready,
    sanitize_terminal_output,
)
from framework.tools.terminal.pty_keys import (
    CursorKeyMode,
    detect_bracketed_paste_mode,
    detect_cursor_key_mode,
    strip_bracketed_paste_mode,
    strip_dsr_and_respond,
    strip_smkx_rmkx,
)
from framework.tools.terminal.results import TerminalRead, TerminalSegment

if TYPE_CHECKING:
    from framework.tools.terminal.backends.base import TerminalBackend
    from framework.tools.terminal.types import ShellInfo


@dataclass
class CommandRecord:
    """A single command execution record."""

    command: str
    output: str
    exit_code: int | None = None
    timestamp: float = field(default_factory=time.time)


@dataclass
class TerminalInfo:
    """Metadata about a terminal session."""

    name: str
    shell_type: str
    is_alive: bool
    last_active: float
    command_count: int
    is_default: bool = False


class TerminalSession:
    """Wraps a TerminalBackend with history, auto-restart, and LRU tracking.

    EXTENSION: Phase 2+ concurrent control:
      - Add _lock: asyncio.Lock for exclusive access
      - Add _input_queue: asyncio.Queue for queueing LLM + user input
      - Add inject_user_input(text) method
    """

    def __init__(
        self,
        name: str,
        backend: TerminalBackend,
        shell_info: ShellInfo,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        max_history: int = 5,
        history_truncate: int = 200,
    ) -> None:
        self.name = name
        self._backend = backend
        self.shell_info = shell_info
        self._cwd = cwd
        self._env = env
        self._max_history = max_history
        self._history_truncate = history_truncate
        self._history: list[CommandRecord] = []
        self.created_at = time.time()
        self.last_active = time.time()
        self._needs_restart = True
        self._busy_after_timeout = False
        self._backend_started = False
        self._last_status: str | None = None
        self.cursor_key_mode: CursorKeyMode = CursorKeyMode.UNKNOWN
        self.bracketed_paste_enabled: bool = False

    async def ensure_started(self) -> None:
        """Start the backend immediately if not already started.

        Used by TerminalTool.open so the visible window appears right away
        instead of waiting for the first execute() call.
        """
        if not await self._backend.is_alive() or self._needs_restart:
            await self._backend.start(
                shell=self.shell_info.path,
                cwd=self._cwd,
                env=self._startup_env(),
            )
            self._backend_started = True
            self._needs_restart = False
            self._busy_after_timeout = False
            await self._backend.drain_startup()
            await self._discard_pending_output()

    @property
    def visible(self) -> bool:
        """Whether this session is backed by a visible OS terminal window."""
        return self._backend.visibility == "visible"

    @property
    def window_title(self) -> str | None:
        """Human-readable OS window title when available."""
        return self._backend.window_title

    async def execute(self, command: str, timeout: float = 60.0) -> str:
        """Execute a command and return output.

        Flow:
        1. Check backend alive, restart if dead (lazy recovery).
        2. Drain startup banner/prompt on a newly started tab.
        3. Send command + platform-appropriate newline to PTY.
        4. Read output until timeout or prompt heuristic.
        5. Record truncated history.
        6. Update last_active.
        """
        if not await self._backend.is_alive() or self._needs_restart:
            await self._backend.start(
                shell=self.shell_info.path,
                cwd=self._cwd,
                env=self._startup_env(),
            )
            self._backend_started = True
            self._needs_restart = False
            self._busy_after_timeout = False
            await self._backend.drain_startup()
            await self._discard_pending_output()
        elif self._busy_after_timeout:
            return (
                "<shell_result>\n"
                "<output></output>\n"
                "<status>busy</status>\n"
                "<message>Previous command timed out and may still be running. "
                "Send ^C via shell tool or terminal.interrupt to stop it.</message>\n"
                "</shell_result>"
            )
        elif self._last_status == "waiting_input":
            # Terminal is waiting for user input (e.g. password prompt).
            # Do NOT clear the input line — the prompt is intentional and
            # any cleanup would destroy the cursor position.
            pass
        elif self.shell_info.family.uses_readline():
            await self._discard_pending_output()
            await self._backend.clear_input_line()
            await asyncio.sleep(0.05)
            await self._discard_pending_output()

        # Detect "exit" — it kills the shell and leaves the PTY dead.
        # Write the command first, then drain any trailing output.
        stripped = command.strip().lower()
        if stripped in ("exit", "logout", "quit"):
            await self._backend.write(command + self.shell_info.family.command_ending())
            await asyncio.sleep(0.3)
            output = await self._drain_on_exit(timeout=3.0)
            self._needs_restart = True
            if await self._backend.is_alive():
                await self._backend.terminate()
            return (
                f"<shell_result>\n"
                f"<output>{xml_escape(output)}</output>\n"
                f"<status>ended</status>\n"
                f"<message>Terminal session ended</message>\n"
                f"</shell_result>"
            )

        # Write the command directly.  Do NOT send a leading \r\n — that
        # creates an empty command on bash which corrupts the readline
        # cursor position.  Drain_startup already ensured a clean prompt.
        # If the shell is in an abnormal state (e.g. >>), _is_waiting_for_input
        # will catch it after the first read and we return waiting_input.
        await self._backend.write(command + self.shell_info.family.command_ending())

        # Read output with timeout.
        # We accumulate all chunks, filter ANSI/DA1 pollution from the
        # *combined* string, then check is_prompt_ready on the clean text.
        # Filtering on the combined string avoids chunk-boundary fragmentation
        # of escape sequences.
        output_parts: list[str] = []
        start_time = time.time()
        timed_out = False
        session_ended = False
        waiting_input = False
        saw_command_activity = True
        while time.time() - start_time < timeout:
            if not await self._backend.is_alive():
                session_ended = True
                break

            chunk = await self._backend.read(timeout=0.3, max_size=65536)
            if chunk:
                output_parts.append(chunk)
                combined = "".join(output_parts)
                # Strip ANSI/DA1 pollution before prompt detection so conpty
                # escape sequences don't corrupt the heuristic.
                clean = _strip_ansi_and_da1(combined)
                if self._has_non_prompt_content(clean):
                    saw_command_activity = True
                if saw_command_activity and is_prompt_ready(clean):
                    break
                if self._is_waiting_for_input(clean):
                    waiting_input = True
                    break
            await asyncio.sleep(0.05)
        else:
            timed_out = True

        output = "".join(output_parts)

        # Sanitize model-facing output — strip ANSI/CSI/DA1/OSC control
        # sequences and carriage-return repaint noise.  Internal prompt
        # detection above used _strip_ansi_and_da1 on raw chunks; this
        # full sanitize is for the string returned to the LLM.
        output = sanitize_terminal_output(output)

        # Structured result for exceptional states; plain text for normal output.
        if timed_out and await self._backend.is_alive():
            self._busy_after_timeout = True
            self._last_status = "timeout"
            return (
                f"<shell_result>\n"
                f"<output>{xml_escape(output)}</output>\n"
                f"<status>timeout</status>\n"
                f"<message>Timed out after {timeout:.0f}s — command may still be running</message>\n"
                f"</shell_result>"
            )
        if waiting_input:
            self._busy_after_timeout = False
            self._last_status = "waiting_input"
            return (
                f"<shell_result>\n"
                f"<output>{xml_escape(output)}</output>\n"
                f"<status>waiting_input</status>\n"
                f"<message>Command is waiting for user input</message>\n"
                f"</shell_result>"
            )
        if session_ended:
            self._busy_after_timeout = False
            self._last_status = "ended"
            return (
                f"<shell_result>\n"
                f"<output>{xml_escape(output)}</output>\n"
                f"<status>ended</status>\n"
                f"<message>Terminal session ended</message>\n"
                f"</shell_result>"
            )

        # Truncate and record
        truncated_cmd = command[:self._history_truncate]
        truncated_out = output[:self._history_truncate]
        record = CommandRecord(
            command=truncated_cmd,
            output=truncated_out,
        )
        self._history.append(record)
        if len(self._history) > self._max_history:
            self._history.pop(0)

        self.last_active = time.time()
        self._busy_after_timeout = False
        self._last_status = "ok"
        return output

    async def _drain_on_exit(self, timeout: float = 3.0) -> str:
        """Drain remaining output after the shell has exited.

        Called when the command is 'exit' / 'logout' / 'quit' so we capture
        any trailing prompt or banner instead of timing out on a dead PTY.
        """
        output_parts: list[str] = []
        start_time = time.time()
        while time.time() - start_time < timeout:
            if not await self._backend.is_alive():
                output_parts.append("\n[Terminal session ended]")
                break
            chunk = await self._backend.read(timeout=0.3, max_size=65536)
            if chunk:
                output_parts.append(chunk)
            else:
                break
            await asyncio.sleep(0.05)
        return "".join(output_parts)

    # Common prompt strings that indicate a command is waiting for user input.
    # Checked case-insensitively against the last non-empty output line.
    _INPUT_PROMPT_MARKERS: tuple[str, ...] = (
        "password", "passphrase", "login:", "username:",
        "user name:", "enter password", "enter passphrase",
        "[y/n]", "[Y/n]", "[yes/no]", "(yes/no)",
        # PIN / token / passcode variants
        "pin:", "token:", "passcode", "code:",
        # 2FA / verification
        "verification code:", "2fa code:", "otp:",
        # Key press / confirmation
        "press any key to continue",
        # File overwrite / replace
        "overwrite", "replace",
        # General confirmation prompts
        "confirm",
        # Password re-entry prompts (already covered by "password" but explicit is clearer)
        "current password", "new password", "retype password", "repeat password",
        # Short yes/no forms
        "(y/n)", "[y/N]", "(Y/n)",
    )

    def _is_waiting_for_input(self, output: str) -> bool:
        """Check if the last non-empty line looks like an input prompt."""
        if not output:
            return False
        # Strip ANSI/DA1 so colour codes don't hide the real last line.
        plain = _strip_ansi_and_da1(output)
        lines = [ln for ln in plain.splitlines() if ln.strip()]
        if not lines:
            return False
        last = lines[-1].lower()
        return any(marker in last for marker in self._INPUT_PROMPT_MARKERS)

    async def _discard_pending_output(self, timeout: float = 0.8) -> None:
        """Discard already-buffered PTY output before opening a command window."""
        deadline = time.monotonic() + timeout
        empty_reads = 0
        while time.monotonic() < deadline and empty_reads < 3:
            chunk = await self._backend.read(timeout=0.05, max_size=65536)
            if chunk:
                empty_reads = 0
            else:
                empty_reads += 1

    def _has_non_prompt_content(self, output: str) -> bool:
        """Return True once output contains more than a prompt repaint."""
        for line in output.splitlines():
            stripped = line.strip()
            if stripped and not is_prompt_ready(stripped):
                return True
        return False

    async def _drain_internal_command(self, timeout: float = 3.0) -> None:
        """Drain output from an internal setup command until the prompt returns."""
        output_parts: list[str] = []
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            chunk = await self._backend.read(timeout=0.3, max_size=65536)
            if chunk:
                output_parts.append(chunk)
                clean = _strip_ansi_and_da1("".join(output_parts))
                if is_prompt_ready(clean):
                    return
            await asyncio.sleep(0.05)

    def _startup_env(self) -> dict[str, str]:
        """Return environment for agent-managed terminal sessions."""
        env = dict(os.environ)
        if self._env:
            env.update(self._env)
        env["GIT_PAGER"] = "cat"
        env["PAGER"] = "cat"
        env["LESS"] = "FRX"
        return env

    def get_history(self) -> list[CommandRecord]:
        """Return command history (newest last)."""
        return list(self._history)

    def get_state(self) -> dict[str, Any]:
        """Return serializable state for persistence."""
        return {
            "name": self.name,
            "shell_type": self.shell_info.name,
            "shell_path": self.shell_info.path,
            "cwd": self._cwd,
            "env": self._env,
            "visible": True,
            "created_at": self.created_at,
            "last_active": self.last_active,
            "history": [
                {
                    "command": rec.command,
                    "output": rec.output,
                    "exit_code": rec.exit_code,
                    "timestamp": rec.timestamp,
                }
                for rec in self._history
            ],
        }

    def restore_state(self, data: dict[str, Any]) -> None:
        """Restore session state from persisted data."""
        self.last_active = data.get("last_active", time.time())
        self.created_at = data.get("created_at", self.created_at)
        self._needs_restart = True
        for rec_data in data.get("history", []):
            self._history.append(CommandRecord(
                command=rec_data["command"],
                output=rec_data["output"],
                exit_code=rec_data.get("exit_code"),
                timestamp=rec_data.get("timestamp", time.time()),
            ))

    async def to_info(self, is_default: bool = False) -> TerminalInfo:
        """Return metadata for list/inspection."""
        alive = await self._backend.is_alive() and not self._needs_restart
        return TerminalInfo(
            name=self.name,
            shell_type=self.shell_info.name,
            is_alive=alive,
            last_active=self.last_active,
            command_count=len(self._history),
            is_default=is_default,
        )

    async def write(self, data: str) -> None:
        """Write raw data to the terminal backend."""
        if not await self._backend.is_alive():
            await self._ensure_backend_alive()
        await self._backend.write(data)

    async def poll_once(self, timeout: float = 0.1, max_size: int = 65536) -> TerminalRead:
        """Read pending output, stripping DECCKM/DSR/bracketed-paste sequences.

        DECCKM (smkx/rmkx) sequences update ``cursor_key_mode`` and are
        removed from the returned output.  DSR (Device Status Report)
        queries are stripped and an automatic CPR response is written back
        to stdin so TUI programs don't hang.  Bracketed-paste mode sequences
        update ``bracketed_paste_enabled`` and are stripped from output.
        """
        read = await self._backend.read_pending(timeout=timeout, max_size=max_size)
        if not read.raw:
            return read

        raw_bytes = read.raw.encode("utf-8", errors="replace")

        # Detect and update cursor key mode
        new_mode = detect_cursor_key_mode(raw_bytes)
        if new_mode is not None:
            self.cursor_key_mode = new_mode

        # Detect and update bracketed paste mode
        bp_mode = detect_bracketed_paste_mode(raw_bytes)
        if bp_mode is not None:
            self.bracketed_paste_enabled = bp_mode

        # Strip smkx/rmkx from output
        cleaned = strip_smkx_rmkx(raw_bytes)

        # Strip bracketed-paste enable/disable from output
        cleaned = strip_bracketed_paste_mode(cleaned)

        # Strip DSR queries and auto-respond with cursor position.
        # Pass None for the writer; we issue the response ourselves
        # so we stay in the async context (backend.write is async).
        cleaned, dsr_count = strip_dsr_and_respond(cleaned, None)
        if dsr_count > 0 and self._backend.stdin_writable():
            response = "\x1b[1;1R" * dsr_count
            await self._backend.write(response)

        clean_str = cleaned.decode("utf-8", errors="replace")
        return TerminalRead(stdout=clean_str, stderr=read.stderr, raw=clean_str)

    async def current_segment(self) -> TerminalSegment:
        """Get the current visible terminal segment."""
        return await self._backend.current_segment()

    async def interrupt(self) -> None:
        """Send Ctrl+C to the terminal."""
        await self._backend.interrupt()

    async def is_alive(self) -> bool:
        """Return True if the backend is alive."""
        return await self._backend.is_alive()

    async def terminate(self) -> None:
        """Terminate the terminal session."""
        await self._backend.terminate()

    async def _ensure_backend_alive(self) -> None:
        """Start the backend if it is not alive."""
        await self._backend.start(
            shell=self.shell_info.path,
            cwd=self._cwd,
            env=self._startup_env(),
        )
        self._backend_started = True
        self._needs_restart = False
        self._busy_after_timeout = False

    async def send_interrupt(self) -> None:
        """Send Ctrl-C (\\x03) to the backend and clear busy state.

        Allows the agent (or user) to interrupt a long-running or timed-out
        command so that the next execute() can start a fresh command.
        """
        await self._backend.write("\x03")
        self._busy_after_timeout = False

    async def close(self) -> None:
        """Terminate the backend gracefully, then force kill if needed."""
        await self._backend.terminate()
        # Give it a moment to terminate gracefully
        await asyncio.sleep(0.5)
        if await self._backend.is_alive():
            await self._backend.kill()
