"""TerminalSession — single named session wrapping a TerminalBackend."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from modex_agent.tools.terminal.prompt import (
    _strip_ansi_and_da1,
    detect_pager_entry,
    extract_last_command_output,
    is_prompt_ready,
    is_waiting_for_input,
    resolve_cursor_line,
)
from modex_agent.tools.terminal.pty_keys import (
    CursorKeyMode,
    detect_bracketed_paste_mode,
    detect_cursor_key_mode,
    strip_bracketed_paste_mode,
    strip_dsr_and_respond,
    strip_smkx_rmkx,
)
from modex_agent.tools.terminal.results import TerminalRead, TerminalSegment
from modex_agent.tools.terminal.types import ShellFamily, TerminalCommandStatus

if TYPE_CHECKING:
    from modex_agent.tools.terminal.backends.base import TerminalBackend
    from modex_agent.tools.terminal.config import TerminalRuntimeConfig
    from modex_agent.tools.terminal.poll_loop import PollResult
    from modex_agent.tools.terminal.types import ShellInfo


@dataclass
class TerminalInfo:
    """Metadata about a terminal session."""

    name: str
    shell_type: str
    is_alive: bool
    last_active: float
    is_default: bool = False
    created_at: float = 0.0


class TerminalSession:
    """Wraps a TerminalBackend with auto-restart, and LRU tracking.

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
    ) -> None:
        self.name = name
        self._backend = backend
        self.shell_info = shell_info
        self._cwd = cwd
        self._env = env
        self.created_at = time.time()
        self.last_active = time.time()
        self._needs_restart = True
        self._busy_after_timeout = False
        self._backend_started = False
        self._last_status: str | None = None
        self.cursor_key_mode: CursorKeyMode = CursorKeyMode.UNKNOWN
        self.bracketed_paste_enabled: bool = False
        self._last_byte_at: float = time.monotonic()
        self._ever_received_bytes: bool = False
        self._command_started_at: float | None = None
        self._expected_state: TerminalCommandStatus | None = None

    def touch_output(self) -> None:
        """Reset the no-output timer. Called when output bytes are received."""
        self._last_byte_at = time.monotonic()
        self._ever_received_bytes = True

    def set_expected_state(self, status: TerminalCommandStatus | None) -> None:
        """Set the expected terminal state after an agent operation."""
        self._expected_state = status

    def apply_outcome(self, result: PollResult) -> None:
        """Update the busy/last_status/command_started state for a poll result.

        Per ADR-0010 Decision 7: the single state-event entry point. Tools call
        this after poll_until_settled; they must NOT poke _busy_after_timeout /
        _last_status / _command_started_at directly. This writes a DIFFERENT slot
        than set_expected_state (which writes _expected_state for interference
        detection) — both coexist.
        """
        from modex_agent.tools.terminal.poll_loop import PollOutcome

        match result.outcome:
            case PollOutcome.PROMPT_DETECTED | PollOutcome.PROCESS_EXIT:
                self._command_started_at = None
                self._busy_after_timeout = False
                self._last_status = "ok"
            case PollOutcome.YIELDED:
                self._last_status = "executing"
            case PollOutcome.INPUT_WAIT:
                self._busy_after_timeout = False
                self._last_status = "waiting_input"
            case PollOutcome.LONG_RUNNING:
                self._last_status = "long_running"
            case PollOutcome.PAGINATED:
                self._busy_after_timeout = False
                self._last_status = "paginated"
            case PollOutcome.STUCK:
                self._command_started_at = None
                self._last_status = None
            case PollOutcome.TIMED_OUT:
                self._busy_after_timeout = True
                self._last_status = "timeout"

    def detect_interference(self, actual: TerminalCommandStatus) -> bool:
        """Detect if actual state diverges from expected (possible user interference).

        Only active for visible terminal sessions.
        """
        if not self.visible or self._expected_state is None:
            return False
        unexpected = {
            (TerminalCommandStatus.EXECUTING, TerminalCommandStatus.IDLE),
            (TerminalCommandStatus.LONG_RUNNING, TerminalCommandStatus.IDLE),
        }
        return (self._expected_state, actual) in unexpected

    async def ensure_started(self, env: dict[str, str] | None = None) -> None:
        """Start the backend immediately if not already started.

        Used by TerminalTool.open so the visible window appears right away
        instead of waiting for the first execute() call.

        When ``env`` is provided and the session is not yet started,
        ``self._env`` is mutated to ``env`` BEFORE the start path runs, so
        ``_startup_env()`` (called below) and ``_ensure_backend_alive()``
        (called by ``write()`` when the PTY dies and restarts) both see the
        injected env. When the session is already alive and needs no
        restart, env is silently ignored — env is injected once at start
        time.
        """
        if not await self._backend.is_alive() or self._needs_restart:
            if env is not None:
                self._env = env
            await self._backend.start(
                shell=self.shell_info.path,
                cwd=self._cwd,
                env=self._startup_env(),
            )
            self._backend_started = True
            self._needs_restart = False
            self._busy_after_timeout = False
            await self._backend.drain_startup()
            override = self._prompt_override_command()
            if override is not None:
                await self._backend.write(override + "\r")
                await self._drain_internal_command(timeout=3.0)
            await self._discard_pending_output()

    @property
    def visible(self) -> bool:
        """Whether this session is backed by a visible OS terminal window."""
        return self._backend.visibility == "visible"

    @property
    def window_title(self) -> str | None:
        """Human-readable OS window title when available."""
        return self._backend.window_title

    @property
    def last_byte_at(self) -> float:
        """Monotonic timestamp of the last raw byte received from the PTY."""
        return self._last_byte_at

    @property
    def busy_after_timeout(self) -> bool:
        """True if the previous command timed out and may still be running."""
        return self._busy_after_timeout

    @property
    def last_status(self) -> str | None:
        """Last known session status string (timeout, waiting_input, etc.)."""
        return self._last_status

    @property
    def backend_started(self) -> bool:
        """True if the backend process was started at least once."""
        return self._backend_started

    @property
    def cwd(self) -> str | None:
        """The directory the backend starts in (None = inherit process CWD)."""
        return self._cwd

    async def _discard_pending_output(self, timeout: float = 0.8) -> None:
        """Discard already-buffered PTY output before opening a command window."""
        deadline = time.monotonic() + timeout
        empty_reads = 0
        while time.monotonic() < deadline and empty_reads < 3:
            chunk = await self._backend.read(timeout=0.05, max_size=65536)
            if chunk:
                self._last_byte_at = time.monotonic()
                self._ever_received_bytes = True
                empty_reads = 0
            else:
                empty_reads += 1

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
        from modex_agent.tools.terminal.env import build_full_env

        return build_full_env(self._env)

    def _prompt_override_command(self) -> str | None:
        """Shell command that forces a detectable prompt.

        User shell configs (oh-my-zsh, Powerlevel10k, Starship) set
        prompts with non-ASCII glyphs (``❯``, ``➜``) that
        ``is_prompt_ready`` cannot match.  This returns a command that
        overrides the prompt AFTER the shell's rc files have loaded,
        so the poll loop can reliably detect prompt-ready state.

        Returns ``None`` for non-readline shells (cmd/powershell).
        """
        family = self.shell_info.family
        if family in (ShellFamily.BASH, ShellFamily.SH):
            return r'export PS1="\u@\h:\w\$ "'
        if family is ShellFamily.ZSH:
            return 'export PROMPT="%n@%m:%~ %# "; export PS1="%n@%m:%~ $ "'
        return None

    def get_state(self) -> dict[str, Any]:
        """Return serializable state for persistence."""
        return {
            "name": self.name,
            "shell_type": self.shell_info.name,
            "shell_path": self.shell_info.path,
            "cwd": self._cwd,
            "env": self._env,
            "visible": self.visible,
            "created_at": self.created_at,
            "last_active": self.last_active,
        }

    def restore_state(self, data: dict[str, Any]) -> None:
        """Restore session state from persisted data."""
        self.last_active = data.get("last_active", time.time())
        self.created_at = data.get("created_at", self.created_at)
        self._needs_restart = True

    async def to_info(self, is_default: bool = False) -> TerminalInfo:
        """Return metadata for list/inspection."""
        alive = await self._backend.is_alive() and not self._needs_restart
        return TerminalInfo(
            name=self.name,
            shell_type=self.shell_info.name,
            is_alive=alive,
            last_active=self.last_active,
            is_default=is_default,
            created_at=self.created_at,
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

        # Track raw byte activity for stuck/executing detection
        self.touch_output()

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

    async def refresh_output(self, timeout: float = 0.1) -> TerminalRead:
        """Read fresh PTY data into internal buffers.

        Safe to call when the backend is dead. Cross-backend: buffer-based
        backends flush socket data, tmux updates diff tracker.
        """
        if not await self.is_alive():
            return TerminalRead()
        return await self.poll_once(timeout=timeout)

    async def command_status(
        self,
        config: TerminalRuntimeConfig | None = None,
    ) -> TerminalCommandStatus:
        """Compute current terminal status using the detection priority rules.

        Priority: COMPLETED > UNKNOWN > WAITING_INPUT > IDLE > PAGINATED >
                  IDLE_INPUT_WAIT > STUCK > LONG_RUNNING > EXECUTING

        ``IDLE_INPUT_WAIT`` is not a separate enum value — it surfaces as
        ``WAITING_INPUT``. The naming here just makes the decision order
        explicit: a session that produced output then fell silent past
        ``input_wait_idle_ms`` is reported as ``WAITING_INPUT`` rather than
        ``STUCK``, because the most common cause of "live process, no output"
        is an unmarked prompt (e.g. ``read -s``).
        """
        from modex_agent.tools.terminal.config import TerminalRuntimeConfig as _Cfg

        cfg = config or _Cfg()

        # 1. Process exit
        if not await self.is_alive():
            self._command_started_at = None
            return TerminalCommandStatus.COMPLETED

        # 2. No data ever received → UNKNOWN (safety net)
        if not self._ever_received_bytes:
            return TerminalCommandStatus.UNKNOWN

        # Refresh to get latest data
        await self.refresh_output(timeout=0.05)

        # 3. Content marker → WAITING_INPUT (fast path)
        segment = await self.current_segment()
        full_text = segment.text if segment.text else ""
        if full_text and is_waiting_for_input(full_text):
            return TerminalCommandStatus.WAITING_INPUT

        # 4. Prompt stable → IDLE
        if segment.is_empty_prompt:
            self._command_started_at = None
            return TerminalCommandStatus.IDLE

        # 5. Pager detection
        cursor = resolve_cursor_line(segment)
        if detect_pager_entry(cursor):
            return TerminalCommandStatus.PAGINATED

        # 6. Idle-based input wait — MUST be before STUCK. A live process
        # that fell silent past input_wait_idle_ms is almost certainly
        # waiting for input (silent prompts like ``read -s``). Gated on
        # ``_command_started_at`` so a freshly-started long-running command
        # that has not produced output yet is not misclassified.
        raw_idle_ms = (time.monotonic() - self._last_byte_at) * 1000
        if self._command_started_at is not None and raw_idle_ms >= cfg.input_wait_idle_ms:
            return TerminalCommandStatus.WAITING_INPUT

        # 7. No-output timeout → STUCK
        if raw_idle_ms >= cfg.no_output_timeout_ms:
            return TerminalCommandStatus.STUCK

        # 8. Long-running
        if self._command_started_at is not None:
            elapsed_ms = (time.monotonic() - self._command_started_at) * 1000
            if elapsed_ms >= cfg.long_running_threshold_ms:
                return TerminalCommandStatus.LONG_RUNNING

        # 9. Active output → EXECUTING
        return TerminalCommandStatus.EXECUTING

    async def last_command_output(self) -> str:
        """Get complete output from the last command to current terminal state.

        Calls refresh_output() first to ensure fresh data, then extracts
        from the second-to-last prompt to the end of the buffer.
        """
        await self.refresh_output(timeout=0.1)
        # Access backend's output buffer for the full text
        raw_text = self._backend.output_buffer_text()
        if not raw_text:
            # Fallback for tmux (no buffer) or empty backends
            segment = await self.current_segment()
            raw_text = segment.text
        return extract_last_command_output(raw_text)

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
        """Send Ctrl-C to the backend and clear busy state.

        Each backend implements interrupt() with its platform-appropriate
        mechanism: pexpect uses sendintr() (os.kill SIGINT), Windows
        backends write CTRL_C through the PTY input stream (matching how
        user keyboard Ctrl+C reaches the shell), tmux forwards it via
        send_keys.  See pty_keys.CTRL_C for the rationale.
        """
        await self._backend.interrupt()
        self._busy_after_timeout = False

    async def submit_command(self, command: str) -> None:
        """Submit a command to the PTY with the shell-appropriate line ending.

        Discards pending output to avoid mixing with the command response.
        Shell cleanup (clear_input_line) is no longer needed here — the
        caller (CommandTool.execute) already guards against busy/dead
        states and the shell is expected to be at a clean prompt.
        """
        self._command_started_at = time.monotonic()
        await self._discard_pending_output()
        self._backend.clear_buffer()
        ending = self.shell_info.family.command_ending()
        await self._backend.write(command + ending)

    async def close(self) -> None:
        """Terminate the backend gracefully, then force kill if needed."""
        await self._backend.terminate()
        # Give it a moment to terminate gracefully
        await asyncio.sleep(0.5)
        if await self._backend.is_alive():
            await self._backend.kill()
