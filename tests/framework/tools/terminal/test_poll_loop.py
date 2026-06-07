"""Test poll_until_settled() PollOutcome branches."""

from __future__ import annotations

import time

import pytest

from framework.tools.terminal.config import TerminalRuntimeConfig
from framework.tools.terminal.poll_loop import PollOutcome, poll_until_settled
from framework.tools.terminal.process_registry import ProcessRegistry
from framework.tools.terminal.results import TerminalRead, TerminalSegment

from tests.framework.tools.terminal.conftest import make_session


def _quick_config(**overrides) -> TerminalRuntimeConfig:
    defaults = dict(
        no_output_timeout_ms=30_000,
        long_running_threshold_ms=300_000,
        prompt_stabilize_ms=50,
        default_yield_ms=500,
        default_command_timeout_seconds=10,
    )
    defaults.update(overrides)
    return TerminalRuntimeConfig(**defaults)


class TestPollProcessExit:
    @pytest.mark.asyncio
    async def test_dead_backend_returns_process_exit(self) -> None:
        session = make_session()
        session._backend._alive = False
        session._ever_received_bytes = True
        registry = ProcessRegistry()
        proc = registry.create(command="echo hi", terminal="test", cwd=None, pid=None)
        config = _quick_config()
        result = await poll_until_settled(
            session, registry, proc.id, config,
            yield_ms=500, timeout_seconds=10,
        )
        assert result.outcome == PollOutcome.PROCESS_EXIT


class TestPollPromptDetected:
    @pytest.mark.asyncio
    async def test_stable_prompt_returns_prompt_detected(self) -> None:
        session = make_session()
        session._ever_received_bytes = True
        session._backend._segment = TerminalSegment(
            text="$ ", cursor_line="$ ", is_empty_prompt=True,
        )
        session._backend._read_queue = [
            TerminalRead(stdout="done\n", raw="done\n"),
        ]
        registry = ProcessRegistry()
        proc = registry.create(command="echo done", terminal="test", cwd=None, pid=None)
        config = _quick_config()
        result = await poll_until_settled(
            session, registry, proc.id, config,
            yield_ms=500, timeout_seconds=10,
        )
        assert result.outcome == PollOutcome.PROMPT_DETECTED


class TestPollInputWait:
    @pytest.mark.asyncio
    async def test_input_marker_returns_input_wait(self) -> None:
        session = make_session()
        session._ever_received_bytes = True
        session._backend._read_queue = [
            TerminalRead(stdout="[sudo] password for user: ", raw="[sudo] password for user: "),
        ]
        registry = ProcessRegistry()
        proc = registry.create(command="sudo ls", terminal="test", cwd=None, pid=None)
        config = _quick_config()
        result = await poll_until_settled(
            session, registry, proc.id, config,
            yield_ms=500, timeout_seconds=10, check_input_wait=True,
        )
        assert result.outcome == PollOutcome.INPUT_WAIT


class TestPollStuck:
    @pytest.mark.asyncio
    async def test_no_output_timeout_returns_stuck(self) -> None:
        session = make_session()
        session._ever_received_bytes = True
        session._last_byte_at = time.monotonic() - 1  # 1s ago
        session._backend._alive = True
        registry = ProcessRegistry()
        proc = registry.create(command="hang", terminal="test", cwd=None, pid=None)
        config = _quick_config(no_output_timeout_ms=200)  # very short
        result = await poll_until_settled(
            session, registry, proc.id, config,
            yield_ms=500, timeout_seconds=10,
        )
        assert result.outcome == PollOutcome.STUCK


class TestPollLongRunning:
    @pytest.mark.asyncio
    async def test_elapsed_over_threshold_returns_long_running(self) -> None:
        session = make_session()
        session._ever_received_bytes = True
        session._last_byte_at = time.monotonic()
        session._backend._alive = True
        # Set non-prompt segment so prompt detection doesn't fire before long-running
        session._backend._segment = TerminalSegment(
            text="building...", cursor_line="building...", is_empty_prompt=False,
        )
        # Queue output so output_received becomes True
        session._backend._read_queue = [
            TerminalRead(stdout="building...\n", raw="building...\n"),
        ]
        registry = ProcessRegistry()
        proc = registry.create(command="make", terminal="test", cwd=None, pid=None)
        config = _quick_config(
            long_running_threshold_ms=100,
            default_yield_ms=500,
        )
        result = await poll_until_settled(
            session, registry, proc.id, config,
            yield_ms=500, timeout_seconds=10,
        )
        assert result.outcome == PollOutcome.LONG_RUNNING


class TestPollYielded:
    @pytest.mark.asyncio
    async def test_yield_window_expires(self) -> None:
        session = make_session()
        session._ever_received_bytes = True
        session._backend._alive = True
        session._backend._read_queue = [
            TerminalRead(stdout="output\n", raw="output\n"),
        ]
        registry = ProcessRegistry()
        proc = registry.create(command="cmd", terminal="test", cwd=None, pid=None)
        config = _quick_config(
            long_running_threshold_ms=300_000,  # very high, won't trigger
        )
        result = await poll_until_settled(
            session, registry, proc.id, config,
            yield_ms=50, timeout_seconds=10,
        )
        assert result.outcome == PollOutcome.YIELDED
