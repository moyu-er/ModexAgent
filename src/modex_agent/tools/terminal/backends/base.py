"""TerminalBackend abstract base class.

Implementations:
- WinptyConsoleWindowBackend (legacy alias: VisibleWindowsPtyBackend):
  Windows subprocess with CREATE_NEW_CONSOLE
- WinptyHiddenBackend (legacy alias: WindowsHiddenPtyBackend):
  Windows in-process pywinpty
- PexpectPtyBackend / TmuxPtyBackend: Unix
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod

from modex_agent.tools.terminal.prompt import drain_windows_startup
from modex_agent.tools.terminal.results import SlidingOutputBuffer, TerminalRead, TerminalSegment
from modex_agent.tools.terminal.types import Platform, ShellFamily, TerminalVisibility


def extract_current_segment_from_buffer(text: str) -> TerminalSegment:
    """Extract the last terminal segment from buffered PTY output.

    Strips ANSI/CSI sequences before checking for prompt endings so that
    terminal control codes (e.g. \\x1b[0K, \\x1b[?25h) after the prompt
    do not prevent empty-prompt detection.

    Uses the same strict prompt detection as ``extract_last_command_output``
    (``is_prompt_ready`` + ``_is_prompt_with_command``) to avoid false
    positives where command output happens to end with ``$`` / ``>`` / ``#``.
    """
    from modex_agent.tools.terminal.prompt import (
        _is_prompt_with_command,
        _strip_ansi_and_da1,
        is_prompt_ready,
    )

    clean = _strip_ansi_and_da1(text)
    lines = clean.splitlines()
    if not lines:
        return TerminalSegment(text="", cursor_line="", is_empty_prompt=True)
    cursor_line = lines[-1]
    prompt_indexes = [
        index
        for index, line in enumerate(lines)
        if is_prompt_ready(line) or _is_prompt_with_command(line)
    ]
    start = prompt_indexes[-1] if prompt_indexes else max(0, len(lines) - 1)
    segment_text = "\n".join(lines[start:])
    # is_empty_prompt: only ``is_prompt_ready`` — ``_is_prompt_with_command``
    # matches lines like "PS C:\\> npm install" which are *not* an empty
    # prompt (a command is already typed after the prompt).
    return TerminalSegment(
        text=segment_text,
        cursor_line=cursor_line,
        is_empty_prompt=is_prompt_ready(cursor_line),
    )


class TerminalBackend(ABC):
    """Abstract terminal backend — wraps platform-specific PTY libraries."""

    def __init__(self) -> None:
        self._output_buffer: SlidingOutputBuffer | None = None

    def mark_command_boundary(self) -> None:
        """Seal current buffer parts as a completed command block."""
        if self._output_buffer is not None:
            self._output_buffer.mark_command_boundary()

    def output_buffer_text(self) -> str:
        """Return the full output buffer text, or empty string if no buffer."""
        if self._output_buffer is not None:
            return self._output_buffer.text
        return ""

    def _append_to_buffer(self, text: str) -> None:
        """Append output text to the sliding output buffer."""
        if self._output_buffer is not None:
            self._output_buffer.append(text)

    def buffer_size(self) -> int:
        """Current buffered output size in chars (0 when no buffer)."""
        return self._output_buffer.total_chars if self._output_buffer is not None else 0

    def clear_buffer(self) -> None:
        """Drop all buffered output (used under memory pressure)."""
        if self._output_buffer is not None:
            self._output_buffer.clear()

    @property
    @abstractmethod
    def platform(self) -> Platform:
        """Platform this backend runs on."""

    @property
    @abstractmethod
    def visibility(self) -> TerminalVisibility:
        """Whether the terminal window is visible to the user."""

    @abstractmethod
    async def start(
        self,
        shell: str | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        """Start the shell process."""

    # ------------------------------------------------------------------
    # Async-safety contract (ADR-0032 D1)
    # ------------------------------------------------------------------
    # Opt-in hooks for synchronous PTY transports. Backends whose
    # underlying ``write`` / ``read`` is blocking implement these; the
    # template methods below wrap them in ``loop.run_in_executor``.
    # Backends with native async I/O (visible-windows post-04) or a
    # snapshot model (tmux post-05) override ``write`` / ``read_pending``
    # directly and never implement the hooks.
    #
    # The hooks default to ``raise NotImplementedError`` so a backend
    # that fails to override either the hook OR the template gets a clear
    # error rather than silent passthrough.

    def _write_blocking(self, data: str) -> None:
        """Blocking write hook for synchronous PTY transports.

        Override in backends whose underlying ``write`` is a blocking
        call (pywinpty ``PtyProcess.write``, pexpect ``send``). The
        base-class ``write`` template wraps this in
        ``loop.run_in_executor(None, ...)``. Backends with native async
        I/O or a snapshot model override ``write`` directly and never
        implement this hook.
        """
        raise NotImplementedError

    def _read_blocking(self, timeout: float, max_size: int) -> str:
        """Blocking read hook for synchronous PTY transports.

        Override in backends whose underlying ``read`` is a blocking
        call (pywinpty socket ``recv``, pexpect ``read_nonblocking``).
        The base-class ``read_pending`` template wraps this in
        ``loop.run_in_executor(None, ...)``. Backends with native async
        I/O or a snapshot model override ``read_pending`` directly and
        never implement this hook.
        """
        raise NotImplementedError

    async def write(self, data: str) -> None:
        """Send input to the PTY via the blocking hook, executor-wrapped.

        Template method (ADR-0032 D1). Backends with synchronous I/O
        implement ``_write_blocking`` and inherit this template; backends
        with native async I/O or a snapshot model override ``write``
        directly.
        """
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._write_blocking, data)

    async def read_pending(self, timeout: float = 5.0, max_size: int = 65536) -> TerminalRead:
        """Read pending PTY output via the blocking hook, executor-wrapped.

        Template method (ADR-0032 D1). Reads raw bytes through
        ``_read_blocking``, appends them to ``self._output_buffer`` when
        non-empty, and returns a ``TerminalRead``. Backends with native
        async I/O or a snapshot model override ``read_pending`` directly.
        """
        loop = asyncio.get_running_loop()

        def _do_read() -> str:
            return self._read_blocking(timeout, max_size)

        try:
            raw = await loop.run_in_executor(None, _do_read)
            if raw and self._output_buffer is not None:
                self._append_to_buffer(raw)
            return TerminalRead(stdout=raw, raw=raw)
        except Exception:
            return TerminalRead(stdout="", raw="")

    async def read(self, timeout: float = 5.0, max_size: int = 65536) -> str:
        """Read PTY output. Non-blocking; returns collected text on timeout.

        Backward-compatible wrapper around read_pending().  Returns the raw
        string for callers that don't need the structured TerminalRead.
        """
        result = await self.read_pending(timeout=timeout, max_size=max_size)
        return result.raw

    def stdin_wait_evidence(self) -> bool | None:
        """Linux kernel probe: is the foreground process group blocked reading stdin?

        None means no evidence is available on this platform.
        """
        return None

    # ------------------------------------------------------------------
    # Shared byte-stream behaviors (ADR-0032 D4)
    # ------------------------------------------------------------------
    # Concrete defaults backed by the ``_shell_family`` abstract hook.
    # Tmux overrides ``current_segment`` / ``drain_startup`` because its
    # snapshot I/O model diverges from the byte-stream path; the three
    # byte-stream backends inherit these defaults.

    @abstractmethod
    def _shell_family(self) -> ShellFamily:
        """Return the shell family of the running shell.

        Abstract hook (ADR-0032 D4.1). Every concrete subclass must
        implement this — typically as a one-liner returning
        ``_family_from_path(self._shell or "")``. The base class uses
        the returned family to gate readline-dependent behaviors
        (``clear_input_line`` / ``drain_startup``).
        """

    async def current_segment(self) -> TerminalSegment:
        """Snapshot the visible terminal content and cursor line.

        Default byte-stream implementation (ADR-0032 D4). Tmux overrides
        to use ``capture_pane()`` because its snapshot I/O model does not
        accumulate into ``self._output_buffer``.
        """
        if self._output_buffer is None:
            return TerminalSegment(text="")
        return extract_current_segment_from_buffer(self._output_buffer.text)

    async def clear_input_line(self) -> None:
        """Clear the current input line without interrupting jobs.

        For readline shells (bash/zsh/sh) this sends Ctrl+A Ctrl+K
        (``\\x01\\x0b``). For non-readline shells (cmd/powershell) this
        is a no-op.
        """
        if self._shell_family().uses_readline():
            await self.write("\x01\x0b")

    async def drain_startup(self) -> None:
        """Wait until the terminal is ready for input (prompt visible).

        Default byte-stream implementation (ADR-0032 D4): delegates to
        the shared ``drain_windows_startup`` helper. Tmux overrides
        because its snapshot I/O model requires ``capture_pane``-based
        prompt detection rather than byte-stream prompt detection.
        """
        await drain_windows_startup(
            read_fn=self.read,
            write_fn=self.write,
            is_alive_fn=self.is_alive,
            uses_readline=self._shell_family().uses_readline(),
        )

    @abstractmethod
    async def interrupt(self) -> None:
        """Send an interrupt signal (Ctrl-C) to the PTY."""

    @abstractmethod
    async def is_alive(self) -> bool:
        """Check if the shell process is still running."""

    @abstractmethod
    async def terminate(self) -> None:
        """Graceful termination (SIGTERM equivalent)."""

    @abstractmethod
    async def kill(self) -> None:
        """Force kill (SIGKILL equivalent)."""

    @abstractmethod
    def stdin_writable(self) -> bool:
        """Return True if stdin is currently writable."""

    @property
    def window_title(self) -> str | None:
        """Human-readable OS window title when the backend has one."""
        return None
