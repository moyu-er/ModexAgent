"""Guard allowlist asymmetry — STUCK is process-writable, not command-writable.

D6 of the terminal-trio split-brain fix: the STUCK suggestion text tells
the agent to ``process write``, but ``_PROCESS_ALLOWED`` rejected STUCK —
the message lied. STUCK's usual causes are unrecognized silent prompts
(custom PS1, password prompts without markers); a new command into a
possibly-hung terminal stays rejected.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from modex_agent.tools.terminal.guard import (
    check_command_writable,
    check_process_writable,
)
from modex_agent.tools.terminal.types import TerminalCommandStatus


def _session_with_status(status: TerminalCommandStatus) -> MagicMock:
    """A stand-in session pinned to *status* with reject-path snapshot
    fields made safe (the diagnostic builder calls len() on output)."""
    session = MagicMock()
    session.command_status = AsyncMock(return_value=status)
    session.current_segment = AsyncMock(return_value=None)
    session.last_command_output = AsyncMock(return_value="")
    session.last_byte_at = 0.0
    session._command_started_at = None
    return session


async def test_process_guard_allows_stuck() -> None:
    session = _session_with_status(TerminalCommandStatus.STUCK)

    result = await check_process_writable(session)

    assert result is None, "STUCK must be process-writable so `process write` works"


async def test_command_guard_still_rejects_stuck() -> None:
    session = _session_with_status(TerminalCommandStatus.STUCK)

    result = await check_command_writable(session)

    assert result is not None, (
        "STUCK must stay command-rejected (new commands into a possibly-hung terminal)"
    )


async def test_process_guard_still_rejects_long_running() -> None:
    session = _session_with_status(TerminalCommandStatus.LONG_RUNNING)

    result = await check_process_writable(session)

    assert result is not None
