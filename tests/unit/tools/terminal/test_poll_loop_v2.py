from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from modex_agent.tools.terminal.config import TerminalRuntimeConfig
from modex_agent.tools.terminal.poll_loop import PollOutcome, poll_until_settled
from modex_agent.tools.terminal.process_registry import ProcessRegistry
from modex_agent.tools.terminal.results import TerminalRead, TerminalSegment
from modex_agent.tools.terminal.session import TerminalSession
from modex_agent.tools.terminal.types import ProcessStatus


def _quiet_session(stdin_wait_evidence: bool) -> MagicMock:
    backend = MagicMock()
    backend.stdin_wait_evidence.return_value = stdin_wait_evidence
    session = MagicMock(spec=TerminalSession)
    session._backend = backend
    session.poll_once = AsyncMock(return_value=TerminalRead())
    session.is_alive = AsyncMock(return_value=True)
    session.current_segment = AsyncMock(return_value=TerminalSegment(text=""))
    return session


async def test_quiet_without_evidence_reaches_deadline_not_input_wait() -> None:
    config = TerminalRuntimeConfig(command_deadline_seconds=2, input_wait_idle_ms=0)
    registry = ProcessRegistry(config)
    proc = registry.create(command="slow", terminal="default", cwd=None, pid=None)

    result = await poll_until_settled(
        _quiet_session(stdin_wait_evidence=False),
        registry,
        proc.id,
        config,
    )

    assert result.outcome is PollOutcome.TIMED_OUT


async def test_quiet_with_kernel_evidence_returns_input_wait() -> None:
    config = TerminalRuntimeConfig(command_deadline_seconds=2, input_wait_idle_ms=0)
    registry = ProcessRegistry(config)
    proc = registry.create(command="read value", terminal="default", cwd=None, pid=None)

    result = await poll_until_settled(
        _quiet_session(stdin_wait_evidence=True),
        registry,
        proc.id,
        config,
    )

    assert result.outcome is PollOutcome.INPUT_WAIT


def _dead_session() -> MagicMock:
    session = MagicMock(spec=TerminalSession)
    session.poll_once = AsyncMock(return_value=TerminalRead())
    session.is_alive = AsyncMock(return_value=False)
    return session


async def test_dead_backend_with_finalized_timeout_returns_timed_out() -> None:
    config = TerminalRuntimeConfig(command_deadline_seconds=2)
    registry = ProcessRegistry(config)
    proc = registry.create(command="slow", terminal="default", cwd=None, pid=None)
    proc.deadline_at = 0
    registry.mark_exited(
        proc.id,
        exit_code=None,
        exit_signal="TIMEOUT",
        status=ProcessStatus.TIMED_OUT,
        timed_out=True,
    )

    result = await poll_until_settled(
        _dead_session(),
        registry,
        proc.id,
        config,
    )

    assert result.outcome is PollOutcome.TIMED_OUT


async def test_dead_backend_before_deadline_returns_process_exit() -> None:
    config = TerminalRuntimeConfig(command_deadline_seconds=2)
    registry = ProcessRegistry(config)
    proc = registry.create(command="true", terminal="default", cwd=None, pid=None)

    result = await poll_until_settled(
        _dead_session(),
        registry,
        proc.id,
        config,
    )

    assert result.outcome is PollOutcome.PROCESS_EXIT


def test_append_output_after_finalization_is_dropped() -> None:
    registry = ProcessRegistry()

    registry.append_output("missing", "stdout", "late output")

    assert registry.list_running() == []
    assert registry.list_finished() == []
