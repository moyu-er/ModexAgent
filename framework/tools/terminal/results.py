from __future__ import annotations

from dataclasses import dataclass

from framework.tools.terminal.types import ProcessStatus


@dataclass(frozen=True)
class TerminalRead:
    stdout: str = ""
    stderr: str = ""
    raw: str = ""


@dataclass(frozen=True)
class TerminalSegment:
    text: str
    cursor_line: str = ""
    is_empty_prompt: bool = False


@dataclass(frozen=True)
class CommandResult:
    status: ProcessStatus
    session_id: str | None
    terminal: str
    output: str
    tail: str
    pid: int | None = None
    cwd: str | None = None
    exit_code: int | None = None
    exit_signal: str | int | None = None
    timed_out: bool = False
    duration_ms: int | None = None
    failure_kind: str | None = None
    message: str | None = None
    started_at: float | None = None
    ended_at: float | None = None
    truncated: bool = False
    stdin_writable: bool | None = None
    waiting_for_input: bool | None = None
    idle_ms: int | None = None


@dataclass(frozen=True)
class ProcessActionResult:
    status: ProcessStatus
    session_id: str | None
    text: str
    details: dict[str, object]
