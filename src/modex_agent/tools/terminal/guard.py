"""Terminal input guard — pre-check before sending commands/writes."""

from __future__ import annotations

import time
from dataclasses import dataclass

from modex_agent.tools.terminal.config import TerminalRuntimeConfig
from modex_agent.tools.terminal.process_registry import ProcessRegistry
from modex_agent.tools.terminal.session import TerminalSession
from modex_agent.tools.terminal.types import TerminalCommandStatus


@dataclass
class TerminalSnapshot:
    """Point-in-time diagnostic snapshot of a terminal session."""

    status: TerminalCommandStatus
    cursor_line: str
    last_output: str
    idle_ms: int
    elapsed_ms: int | None
    suggestion: str


@dataclass
class TerminalGuardResult:
    """Guard rejection result with reason and diagnostic snapshot."""

    status: TerminalCommandStatus
    message: str
    snapshot: TerminalSnapshot


_SUGGESTIONS: dict[TerminalCommandStatus, str] = {
    TerminalCommandStatus.EXECUTING: (
        "A command is still running in this tab. Use process (^C to interrupt, or send input), "
        "or wait."
    ),
    TerminalCommandStatus.WAITING_INPUT: (
        "The previous command may be waiting for input. Answer it with the process tool first."
    ),
}

_MESSAGES: dict[TerminalCommandStatus, str] = {
    TerminalCommandStatus.EXECUTING: "Terminal is not ready: a command is still executing.",
    TerminalCommandStatus.WAITING_INPUT: (
        "Terminal is not ready: the previous command may be waiting for input."
    ),
}

_COMMAND_ALLOWED: frozenset[TerminalCommandStatus] = frozenset(
    {
        TerminalCommandStatus.IDLE,
        TerminalCommandStatus.UNKNOWN,
        TerminalCommandStatus.COMPLETED,
        TerminalCommandStatus.TIMED_OUT,
    }
)

_PROCESS_ALLOWED = _COMMAND_ALLOWED | frozenset({TerminalCommandStatus.WAITING_INPUT})


async def check_process_writable(
    session: TerminalSession,
    config: TerminalRuntimeConfig | None = None,
    registry: ProcessRegistry | None = None,
) -> TerminalGuardResult | None:
    """Guard for ProcessTool: allow interaction with running processes.

    WAITING_INPUT is allowed so ProcessTool can answer interactive commands.
    EXECUTING is allowed when the running process has not produced any
    output — this covers silent stdin consumers like ``cat > file`` that
    never print a prompt but are waiting for input.
    EXECUTING with prior output (e.g. build output) is still rejected
    to avoid injecting data into a command that isn't expecting it.
    """
    cfg = config or TerminalRuntimeConfig()
    result = await _check_writable(session, _PROCESS_ALLOWED, cfg)
    if result is None:
        return None

    if result.status == TerminalCommandStatus.EXECUTING and registry is not None:
        running = registry.get_running_by_terminal(session.name)
        if running is not None:
            idle_ms = registry.idle_ms(running.id)
            if idle_ms is not None and idle_ms >= 1000:
                return None

    return result


async def _check_writable(
    session: TerminalSession,
    allowed: frozenset[TerminalCommandStatus],
    config: TerminalRuntimeConfig | None = None,
) -> TerminalGuardResult | None:
    """Generic guard — returns None if status is in *allowed*, else diagnostic."""
    cfg = config or TerminalRuntimeConfig()
    status = await session.command_status(config=cfg)

    if status in allowed:
        return None

    # Build diagnostic snapshot
    segment = await session.current_segment()
    cursor = segment.cursor_line if segment else ""

    output = await session.last_command_output()
    if len(output) > 2000:
        output = output[:2000] + "...(truncated)"

    raw_idle_ms = int((time.monotonic() - session.last_byte_at) * 1000)

    elapsed_ms: int | None = None
    if session._command_started_at is not None:
        elapsed_ms = int((time.monotonic() - session._command_started_at) * 1000)

    snapshot = TerminalSnapshot(
        status=status,
        cursor_line=cursor,
        last_output=output,
        idle_ms=raw_idle_ms,
        elapsed_ms=elapsed_ms,
        suggestion=_SUGGESTIONS.get(status, ""),
    )

    return TerminalGuardResult(
        status=status,
        message=_MESSAGES.get(status, f"Terminal is not ready: state is {status.value}."),
        snapshot=snapshot,
    )


async def check_command_writable(
    session: TerminalSession,
    config: TerminalRuntimeConfig | None = None,
) -> TerminalGuardResult | None:
    """Guard for CommandTool: only allow submission when terminal is truly idle.

    WAITING_INPUT is rejected because a new command would overwrite the interaction.
    """
    return await _check_writable(session, _COMMAND_ALLOWED, config)
