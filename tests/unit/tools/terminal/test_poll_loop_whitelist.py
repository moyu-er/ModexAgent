"""Unit tests for poll_loop no-output whitelist (sleep command handling).

The whitelist lets commands like ``sleep 60`` run to completion (or hard
timeout) without being misclassified as STUCK after ``no_output_timeout_ms``.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from modex_agent.tools.terminal.config import TerminalRuntimeConfig
from modex_agent.tools.terminal.poll_loop import PollOutcome, poll_until_settled
from modex_agent.tools.terminal.results import TerminalRead
from modex_agent.tools.terminal.session import TerminalSession


def _make_session(dead: bool = False) -> MagicMock:
    session = MagicMock(spec=TerminalSession)
    session.last_byte_at = time.monotonic()
    session.poll_once = AsyncMock(return_value=TerminalRead())
    session.is_alive = AsyncMock(return_value=not dead)
    session.current_segment = AsyncMock(return_value=MagicMock(is_empty_prompt=False, cursor_line=""))
    return session


def _make_registry() -> MagicMock:
    reg = MagicMock()
    reg.append_output = MagicMock()
    reg.running_runtime = MagicMock(return_value=None)
    return reg


def _config(*, no_output_timeout_ms: int = 500, timeout_seconds: int = 5) -> TerminalRuntimeConfig:
    return TerminalRuntimeConfig(
        no_output_timeout_ms=no_output_timeout_ms,
        default_command_timeout_seconds=timeout_seconds,
        prompt_stabilize_ms=50,
        default_yield_ms=10_000,
        long_running_threshold_ms=999_000,
    )


@pytest.mark.asyncio
async def test_non_whitelisted_command_hits_stuck() -> None:
    session = _make_session()
    reg = _make_registry()
    cfg = _config(no_output_timeout_ms=300, timeout_seconds=5)

    result = await poll_until_settled(
        session, reg, "ps-test", cfg,
        yield_ms=999_000, timeout_seconds=5, command="some_long_silent_cmd",
    )
    assert result.outcome == PollOutcome.STUCK


@pytest.mark.asyncio
async def test_sleep_command_skips_stuck() -> None:
    session = _make_session()
    reg = _make_registry()
    cfg = _config(no_output_timeout_ms=300, timeout_seconds=2)

    result = await poll_until_settled(
        session, reg, "ps-test", cfg,
        yield_ms=999_000, timeout_seconds=2, command="sleep 60",
    )
    assert result.outcome == PollOutcome.TIMED_OUT


@pytest.mark.asyncio
async def test_sleep_with_flags_skips_stuck() -> None:
    session = _make_session()
    reg = _make_registry()
    cfg = _config(no_output_timeout_ms=300, timeout_seconds=1)

    result = await poll_until_settled(
        session, reg, "ps-test", cfg,
        yield_ms=999_000, timeout_seconds=1, command="sleep --quiet 30",
    )
    assert result.outcome == PollOutcome.TIMED_OUT


@pytest.mark.asyncio
async def test_sleep_completes_when_process_exits() -> None:
    session = _make_session()
    reg = _make_registry()
    cfg = _config(no_output_timeout_ms=300, timeout_seconds=5)

    call_count = 0
    async def _is_alive():
        nonlocal call_count
        call_count += 1
        return call_count < 3
    session.is_alive = _is_alive

    result = await poll_until_settled(
        session, reg, "ps-test", cfg,
        yield_ms=999_000, timeout_seconds=5, command="sleep 0.1",
    )
    assert result.outcome == PollOutcome.PROCESS_EXIT


@pytest.mark.asyncio
async def test_custom_whitelist_pattern() -> None:
    session = _make_session()
    reg = _make_registry()
    cfg = TerminalRuntimeConfig(
        no_output_timeout_ms=300,
        default_command_timeout_seconds=1,
        prompt_stabilize_ms=50,
        default_yield_ms=999_000,
        long_running_threshold_ms=999_000,
        no_output_whitelist=(r"^\s*my_silent_tool\s",),
    )

    result = await poll_until_settled(
        session, reg, "ps-test", cfg,
        yield_ms=999_000, timeout_seconds=1, command="my_silent_tool --wait",
    )
    assert result.outcome == PollOutcome.TIMED_OUT


@pytest.mark.asyncio
async def test_empty_command_does_not_skip_stuck() -> None:
    session = _make_session()
    reg = _make_registry()
    cfg = _config(no_output_timeout_ms=300, timeout_seconds=5)

    result = await poll_until_settled(
        session, reg, "ps-test", cfg,
        yield_ms=999_000, timeout_seconds=5, command="",
    )
    assert result.outcome == PollOutcome.STUCK


@pytest.mark.asyncio
async def test_process_drain_whitelisted_command_skips_stuck() -> None:
    """process write/submit/paste drain path also respects the whitelist.

    When the running command is ``sleep``, _drain_terminal_after_action
    forwards ``command=running.command`` to poll_until_settled, so the
    STUCK check is skipped — same as CommandTool.execute.
    """
    from modex_agent.tools.terminal.process_tool import _drain_terminal_after_action

    session = _make_session()
    reg = _make_registry()
    cfg = _config(no_output_timeout_ms=300, timeout_seconds=1)

    output, result = await _drain_terminal_after_action(
        session, reg, "ps-test", cfg, command="sleep 60",
    )
    assert result.outcome == PollOutcome.TIMED_OUT


@pytest.mark.asyncio
async def test_process_drain_non_whitelisted_hits_stuck() -> None:
    """process drain on a non-whitelisted command still hits STUCK."""
    from modex_agent.tools.terminal.process_tool import _drain_terminal_after_action

    session = _make_session()
    reg = _make_registry()
    cfg = _config(no_output_timeout_ms=300, timeout_seconds=5)

    output, result = await _drain_terminal_after_action(
        session, reg, "ps-test", cfg, command="some_silent_cmd",
    )
    assert result.outcome == PollOutcome.STUCK
