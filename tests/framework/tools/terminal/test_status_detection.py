"""Test TerminalSession.command_status() detection logic."""

from __future__ import annotations

import time

import pytest

from framework.tools.terminal.config import TerminalRuntimeConfig
from framework.tools.terminal.results import TerminalSegment
from framework.tools.terminal.types import TerminalCommandStatus

from tests.framework.tools.terminal.conftest import make_session


@pytest.fixture
def config() -> TerminalRuntimeConfig:
    return TerminalRuntimeConfig(
        no_output_timeout_ms=30_000,
        long_running_threshold_ms=300_000,
    )


class TestCommandStatus:
    """Verify command_status() returns correct state for each scenario."""

    @pytest.mark.asyncio
    async def test_dead_backend_returns_completed(self, config: TerminalRuntimeConfig) -> None:
        session = make_session()
        session._backend._alive = False
        assert await session.command_status(config=config) == TerminalCommandStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_no_bytes_returns_unknown(self, config: TerminalRuntimeConfig) -> None:
        session = make_session()
        session._ever_received_bytes = False
        assert await session.command_status(config=config) == TerminalCommandStatus.UNKNOWN

    @pytest.mark.asyncio
    async def test_input_marker_returns_waiting_input(self, config: TerminalRuntimeConfig) -> None:
        session = make_session()
        session._ever_received_bytes = True
        session._backend._segment = TerminalSegment(
            text="Password: ", cursor_line="Password: ", is_empty_prompt=False,
        )
        assert await session.command_status(config=config) == TerminalCommandStatus.WAITING_INPUT

    @pytest.mark.asyncio
    async def test_stable_prompt_returns_idle(self, config: TerminalRuntimeConfig) -> None:
        session = make_session()
        session._ever_received_bytes = True
        session._backend._segment = TerminalSegment(
            text="$ ", cursor_line="$ ", is_empty_prompt=True,
        )
        assert await session.command_status(config=config) == TerminalCommandStatus.IDLE

    @pytest.mark.asyncio
    async def test_idle_above_threshold_returns_stuck(self, config: TerminalRuntimeConfig) -> None:
        session = make_session()
        session._ever_received_bytes = True
        session._last_byte_at = time.monotonic() - 35  # 35s ago, > 30s threshold
        session._backend._segment = TerminalSegment(
            text="some output", cursor_line="some output", is_empty_prompt=False,
        )
        assert await session.command_status(config=config) == TerminalCommandStatus.STUCK

    @pytest.mark.asyncio
    async def test_idle_below_threshold_returns_executing(self, config: TerminalRuntimeConfig) -> None:
        session = make_session()
        session._ever_received_bytes = True
        session._last_byte_at = time.monotonic() - 5  # 5s ago, < 30s threshold
        session._backend._segment = TerminalSegment(
            text="some output", cursor_line="some output", is_empty_prompt=False,
        )
        assert await session.command_status(config=config) == TerminalCommandStatus.EXECUTING

    @pytest.mark.asyncio
    async def test_long_running_detected(self) -> None:
        cfg = TerminalRuntimeConfig(
            no_output_timeout_ms=30_000,
            long_running_threshold_ms=100,  # very short for testing
        )
        session = make_session()
        session._ever_received_bytes = True
        session._last_byte_at = time.monotonic() - 5  # recent enough (not STUCK)
        session._command_started_at = time.monotonic() - 0.2  # 200ms > 100ms threshold
        session._backend._segment = TerminalSegment(
            text="output...", cursor_line="output...", is_empty_prompt=False,
        )
        assert await session.command_status(config=cfg) == TerminalCommandStatus.LONG_RUNNING

    @pytest.mark.asyncio
    async def test_idle_resets_command_started_at(self, config: TerminalRuntimeConfig) -> None:
        """When IDLE detected, _command_started_at should be cleared."""
        session = make_session()
        session._ever_received_bytes = True
        session._command_started_at = time.monotonic() - 500
        session._backend._segment = TerminalSegment(
            text="$ ", cursor_line="$ ", is_empty_prompt=True,
        )
        await session.command_status(config=config)
        assert session._command_started_at is None

    @pytest.mark.asyncio
    async def test_no_command_started_at_skips_long_running(self, config: TerminalRuntimeConfig) -> None:
        """Without _command_started_at, LONG_RUNNING is never returned."""
        cfg = TerminalRuntimeConfig(
            no_output_timeout_ms=30_000,
            long_running_threshold_ms=100,
        )
        session = make_session()
        session._ever_received_bytes = True
        session._last_byte_at = time.monotonic() - 1
        session._command_started_at = None  # No command registered
        session._backend._segment = TerminalSegment(
            text="output", cursor_line="output", is_empty_prompt=False,
        )
        assert await session.command_status(config=cfg) == TerminalCommandStatus.EXECUTING
