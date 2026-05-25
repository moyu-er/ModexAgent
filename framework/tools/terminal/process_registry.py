from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from typing import Literal

from framework.tools.terminal.config import TerminalRuntimeConfig
from framework.tools.terminal.pty_keys import CursorKeyMode
from framework.tools.terminal.results import TerminalRead
from framework.tools.terminal.types import ProcessStatus

StreamName = Literal["stdout", "stderr"]


@dataclass
class OutputVelocity:
    chunks_in_window: int = 0
    is_active: bool = False


@dataclass
class RunningSessionRuntime:
    stdin_writable: bool
    waiting_for_input: bool
    idle_ms: int
    last_output_at: float
    output_velocity: OutputVelocity = field(default_factory=OutputVelocity)


@dataclass
class ProcessSession:
    id: str
    terminal: str
    command: str
    pid: int | None
    cwd: str | None
    started_at: float
    status: ProcessStatus = ProcessStatus.RUNNING
    stdin_writable: bool = True
    last_output_at: float = field(default_factory=time.time)
    pending_stdout: list[str] = field(default_factory=list)
    pending_stderr: list[str] = field(default_factory=list)
    aggregated: str = ""
    tail: str = ""
    total_output_chars: int = 0
    max_output_chars: int = 200_000
    pending_max_output_chars: int = 30_000
    truncated: bool = False
    ended_at: float | None = None
    exit_code: int | None = None
    exit_signal: str | int | None = None
    timed_out: bool = False
    failure_kind: str | None = None
    cursor_key_mode: CursorKeyMode = CursorKeyMode.UNKNOWN
    _output_timestamps: list[float] = field(default_factory=list)


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
            max_output_chars=self._config.max_output_chars,
            pending_max_output_chars=self._config.pending_max_output_chars,
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

    def append_output(self, session_id: str, stream: StreamName, chunk: str) -> None:
        session = self._running[session_id]
        now = time.time()
        session.last_output_at = now
        session._output_timestamps.append(now)
        session.total_output_chars += len(chunk)
        pending = session.pending_stdout if stream == "stdout" else session.pending_stderr
        pending.append(chunk)
        self._cap_pending(pending, session)
        combined = session.aggregated + chunk
        if len(combined) > session.max_output_chars:
            session.truncated = True
            combined = combined[-session.max_output_chars :]
        session.aggregated = combined
        session.tail = combined[-2000:]

    def drain_pending(self, session_id: str) -> TerminalRead:
        session = self._running.get(session_id) or self._finished.get(session_id)
        if session is None:
            return TerminalRead()
        stdout = "".join(session.pending_stdout)
        stderr = "".join(session.pending_stderr)
        session.pending_stdout.clear()
        session.pending_stderr.clear()
        return TerminalRead(stdout=stdout, stderr=stderr, raw=stdout + stderr)

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

    def running_runtime(self, session_id: str) -> RunningSessionRuntime | None:
        session = self._running.get(session_id)
        if session is None:
            return None
        now = time.time()
        idle_ms = max(0, int((now - session.last_output_at) * 1000))
        velocity = self._compute_velocity(session, now)

        # Formal idle threshold detection
        formal_waiting = session.stdin_writable and idle_ms >= self._config.input_wait_idle_ms

        # Early detection: consecutive empty-read equivalent via elapsed time + velocity
        elapsed_since_output_ms = idle_ms
        early_waiting = (
            session.stdin_writable
            and not velocity.is_active
            and elapsed_since_output_ms >= self._config.input_wait_early_min_elapsed_ms
        )

        return RunningSessionRuntime(
            stdin_writable=session.stdin_writable,
            waiting_for_input=formal_waiting or early_waiting,
            idle_ms=idle_ms,
            last_output_at=session.last_output_at,
            output_velocity=velocity,
        )

    def _compute_velocity(self, session: ProcessSession, now: float) -> OutputVelocity:
        window = self._config.output_velocity_window_s
        cutoff = now - window
        timestamps = [t for t in session._output_timestamps if t >= cutoff]
        count = len(timestamps)
        is_active = count >= self._config.output_velocity_active_threshold
        # Prune old timestamps to avoid unbounded growth
        if len(session._output_timestamps) > 100:
            session._output_timestamps = timestamps
        return OutputVelocity(chunks_in_window=count, is_active=is_active)

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

    def _cap_pending(self, pending: list[str], session: ProcessSession) -> None:
        total = sum(len(item) for item in pending)
        if total <= session.pending_max_output_chars:
            return
        session.truncated = True
        text = "".join(pending)[-session.pending_max_output_chars :]
        pending.clear()
        pending.append(text)
