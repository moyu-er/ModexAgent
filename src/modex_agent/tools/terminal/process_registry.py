from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field

from modex_agent.tools.terminal.config import TerminalRuntimeConfig
from modex_agent.tools.terminal.pty_keys import CursorKeyMode
from modex_agent.tools.terminal.types import ProcessStatus


@dataclass
class ProcessSession:
    id: str
    terminal: str
    command: str
    pid: int | None
    cwd: str | None
    started_at: float
    deadline_at: float
    status: ProcessStatus = ProcessStatus.RUNNING
    stdin_writable: bool = True
    last_output_at: float = field(default_factory=time.time)
    ended_at: float | None = None
    exit_code: int | None = None
    exit_signal: str | int | None = None
    timed_out: bool = False
    failure_kind: str | None = None
    cursor_key_mode: CursorKeyMode = CursorKeyMode.UNKNOWN


class ProcessRegistry:
    def __init__(self, config: TerminalRuntimeConfig | None = None) -> None:
        self._config = config or TerminalRuntimeConfig()
        self._running: dict[str, ProcessSession] = {}
        self._finished: dict[str, ProcessSession] = {}

    def create(
        self, *, command: str, terminal: str, cwd: str | None, pid: int | None
    ) -> ProcessSession:
        session_id = self._new_id()
        session = ProcessSession(
            id=session_id,
            terminal=terminal,
            command=command,
            pid=pid,
            cwd=cwd,
            started_at=time.time(),
            deadline_at=time.monotonic() + self._config.command_deadline_seconds,
        )
        self._running[session_id] = session
        return session

    def get_running(self, session_id: str) -> ProcessSession | None:
        return self._running.get(session_id)

    def get_finished(self, session_id: str) -> ProcessSession | None:
        return self._finished.get(session_id)

    def get_running_by_terminal(self, terminal_name: str) -> ProcessSession | None:
        """Find the most recent running session for a terminal."""
        for session in reversed(list(self._running.values())):
            if session.terminal == terminal_name:
                return session
        return None

    def get_finished_by_terminal(self, terminal_name: str) -> ProcessSession | None:
        """Find the most recent finished session for a terminal."""
        for session in reversed(list(self._finished.values())):
            if session.terminal == terminal_name:
                return session
        return None

    def list_running(self) -> list[ProcessSession]:
        return sorted(self._running.values(), key=lambda item: item.started_at, reverse=True)

    def list_finished(self) -> list[ProcessSession]:
        return sorted(self._finished.values(), key=lambda item: item.started_at, reverse=True)

    def delete(self, session_id: str) -> bool:
        existed = session_id in self._running or session_id in self._finished
        self._running.pop(session_id, None)
        self._finished.pop(session_id, None)
        return existed

    def record_output(self, session_id: str) -> None:
        """Mark *session_id* as having produced output just now (idle timer).

        Output aggregation lives elsewhere: the poll loop accumulates the
        model-visible parts, and oversize results are truncated by the
        framework overflow interceptor — this registry only tracks liveness.
        """
        session = self._running.get(session_id)
        if session is not None:
            session.last_output_at = time.time()

    def mark_exited(
        self,
        session_id: str,
        *,
        exit_code: int | None,
        exit_signal: str | int | None,
        status: ProcessStatus,
        timed_out: bool = False,
        failure_kind: str | None = None,
    ) -> ProcessSession | None:
        session = self._running.pop(session_id, None)
        if session is None:
            return None
        session.status = status
        session.exit_code = exit_code
        session.exit_signal = exit_signal
        session.ended_at = time.time()
        session.timed_out = timed_out
        session.failure_kind = failure_kind
        self._finished[session_id] = session
        return session

    def refresh_deadline(self, session_id: str) -> None:
        session = self._running.get(session_id)
        if session is not None:
            session.deadline_at = time.monotonic() + self._config.command_deadline_seconds

    def idle_ms(self, session_id: str) -> int | None:
        session = self._running.get(session_id)
        if session is None:
            return None
        return max(0, int((time.time() - session.last_output_at) * 1000))

    def prune_finished(self) -> None:
        cutoff = time.time() - (self._config.finished_ttl_ms / 1000)
        expired = [
            session_id
            for session_id, session in self._finished.items()
            if session.ended_at is not None and session.ended_at < cutoff
        ]
        for session_id in expired:
            self._finished.pop(session_id, None)

    def _new_id(self) -> str:
        while True:
            session_id = f"ps-{secrets.token_hex(4)}"
            if session_id not in self._running and session_id not in self._finished:
                return session_id
