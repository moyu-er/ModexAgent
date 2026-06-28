"""Session.apply_outcome — single state-event entry point (ADR-0010 Decision 7)."""

from __future__ import annotations

from modex_agent.tools.terminal.poll_loop import PollOutcome, PollResult
from modex_agent.tools.terminal.session import TerminalSession
from modex_agent.tools.terminal.types import Platform, ShellFamily, ShellInfo
from tests.framework.tools.terminal.conftest import FakeBackend  # type: ignore


def _make_session() -> TerminalSession:
    backend = FakeBackend()
    shell_info = ShellInfo(family=ShellFamily.BASH, path="/bin/bash", platform=Platform.LINUX)
    return TerminalSession(name="t", backend=backend, shell_info=shell_info)


def _result(outcome: PollOutcome) -> PollResult:
    return PollResult(outcome=outcome, output_parts=["x"], elapsed_ms=10)


def test_apply_outcome_prompt_detected_clears_command_started_and_busy() -> None:
    session = _make_session()
    session._command_started_at = 1.0
    session._busy_after_timeout = True
    session.apply_outcome(_result(PollOutcome.PROMPT_DETECTED))
    assert session._command_started_at is None
    assert session._busy_after_timeout is False
    assert session._last_status == "ok"


def test_apply_outcome_process_exit_clears_like_ok() -> None:
    session = _make_session()
    session._command_started_at = 1.0
    session.apply_outcome(_result(PollOutcome.PROCESS_EXIT))
    assert session._command_started_at is None
    assert session._last_status == "ok"


def test_apply_outcome_input_wait_sets_waiting_input_status() -> None:
    session = _make_session()
    session.apply_outcome(_result(PollOutcome.INPUT_WAIT))
    assert session._last_status == "waiting_input"
    assert session._busy_after_timeout is False


def test_apply_outcome_paginated_sets_paginated_status() -> None:
    session = _make_session()
    session.apply_outcome(_result(PollOutcome.PAGINATED))
    assert session._last_status == "paginated"
    assert session._busy_after_timeout is False


def test_apply_outcome_long_running_keeps_command_started() -> None:
    session = _make_session()
    session._command_started_at = 1.0
    session.apply_outcome(_result(PollOutcome.LONG_RUNNING))
    assert session._command_started_at is not None
    assert session._last_status == "long_running"


def test_apply_outcome_stuck_clears_command_started() -> None:
    session = _make_session()
    session._command_started_at = 1.0
    session.apply_outcome(_result(PollOutcome.STUCK))
    assert session._command_started_at is None
    assert session._last_status is None


def test_apply_outcome_timed_out_sets_busy_after_timeout() -> None:
    """Regression: legacy Session.execute set _busy_after_timeout=True + 'timeout';
    the pre-Phase-5 CommandTool path only cleared _expected_state and missed these."""
    session = _make_session()
    session.apply_outcome(_result(PollOutcome.TIMED_OUT))
    assert session._busy_after_timeout is True
    assert session._last_status == "timeout"


def test_apply_outcome_yielded_sets_executing_keeps_command_started() -> None:
    session = _make_session()
    session._command_started_at = 1.0
    session.apply_outcome(_result(PollOutcome.YIELDED))
    assert session._command_started_at is not None
    assert session._last_status == "executing"


def test_init_expected_state_is_none() -> None:
    """_expected_state is initialised so detect_interference never AttributeErrors."""
    session = _make_session()
    assert session._expected_state is None
