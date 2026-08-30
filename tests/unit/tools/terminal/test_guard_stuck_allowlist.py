from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from modex_agent.tools.terminal.guard import (
    check_command_writable,
    check_process_writable,
)
from modex_agent.tools.terminal.process_registry import ProcessRegistry
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


@pytest.mark.parametrize(
    "status",
    [
        TerminalCommandStatus.IDLE,
        TerminalCommandStatus.UNKNOWN,
        TerminalCommandStatus.COMPLETED,
        TerminalCommandStatus.TIMED_OUT,
    ],
)
async def test_both_guards_allow_settled_states(status: TerminalCommandStatus) -> None:
    session = _session_with_status(status)

    command_result = await check_command_writable(session)
    process_result = await check_process_writable(session)

    assert command_result is None
    assert process_result is None


async def test_waiting_input_is_process_writable_only() -> None:
    session = _session_with_status(TerminalCommandStatus.WAITING_INPUT)

    command_result = await check_command_writable(session)
    process_result = await check_process_writable(session)

    assert command_result is not None
    assert process_result is None


async def test_executing_is_rejected_by_both_guards() -> None:
    session = _session_with_status(TerminalCommandStatus.EXECUTING)

    command_result = await check_command_writable(session)
    process_result = await check_process_writable(session)

    assert command_result is not None
    assert process_result is not None


async def test_process_guard_allows_quiet_executing_stdin_consumer() -> None:
    session = _session_with_status(TerminalCommandStatus.EXECUTING)
    session.name = "default"
    registry = ProcessRegistry()
    running = registry.create(command="cat > file", terminal="default", cwd=None, pid=None)
    running.last_output_at -= 1.1

    result = await check_process_writable(session, registry=registry)

    assert result is None
