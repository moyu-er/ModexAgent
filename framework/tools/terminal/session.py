"""TerminalSession — single named session wrapping a TerminalBackend."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from framework.tools.standard.shell_tool import ShellInfo
    from framework.tools.terminal.backends.base import TerminalBackend


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
    ):
        self.name = name
        self._backend = backend
        self.shell_info = shell_info
        self._cwd = cwd
        self._env = env
        self._max_history = max_history
        self._history_truncate = history_truncate
        self._history: list[CommandRecord] = []
        self.last_active = time.time()
        self._needs_restart = True

    async def execute(self, command: str, timeout: float = 60.0) -> str:
        """Execute a command and return output.

        Flow:
        1. Check backend alive, restart if dead (lazy recovery).
        2. Send command + newline to PTY.
        3. Read output until timeout or prompt heuristic.
        4. Record truncated history.
        5. Update last_active.
        """
        if not await self._backend.is_alive() or self._needs_restart:
            await self._backend.start(
                shell=self.shell_info.path,
                cwd=self._cwd,
                env=self._env,
            )
            self._needs_restart = False

        await self._backend.write(command + "\n")

        # Read output with timeout
        output_parts: list[str] = []
        start_time = time.time()
        while time.time() - start_time < timeout:
            chunk = await self._backend.read(timeout=0.5, max_size=65536)
            if chunk:
                output_parts.append(chunk)
            # Simple heuristic: if we see a prompt-like ending, break early
            combined = "".join(output_parts)
            if combined.rstrip().endswith(("$ ", "# ", "> ")):
                break
            await asyncio.sleep(0.1)

        output = "".join(output_parts)

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
        return output

    def get_history(self) -> list[CommandRecord]:
        """Return command history (newest last)."""
        return list(self._history)

    def to_info(self) -> TerminalInfo:
        """Return metadata for list/inspection."""
        return TerminalInfo(
            name=self.name,
            shell_type=self.shell_info.name,
            is_alive=True,
            last_active=self.last_active,
            command_count=len(self._history),
        )

    async def close(self) -> None:
        """Terminate the backend gracefully, then force kill if needed."""
        await self._backend.terminate()
        # Give it a moment to terminate gracefully
        await asyncio.sleep(0.5)
        if await self._backend.is_alive():
            await self._backend.kill()
