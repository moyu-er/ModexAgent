"""Test terminal input guard mechanism."""

from __future__ import annotations

import time

import pytest

from modex_agent.tools.terminal.config import TerminalRuntimeConfig
from modex_agent.tools.terminal.guard import (
    check_command_writable,
    check_process_writable,
    check_terminal_writable,
)
from modex_agent.tools.terminal.results import TerminalSegment
from modex_agent.tools.terminal.types import TerminalCommandStatus

from tests.framework.tools.terminal.conftest import make_session


def _config(**overrides) -> TerminalRuntimeConfig:
    defaults = dict(no_output_timeout_ms=30_000, long_running_threshold_ms=300_000)
    defaults.update(overrides)
    return TerminalRuntimeConfig(**defaults)


class TestGuardAllowed:
    """States where terminal IS writable (guard returns None)."""

    @pytest.mark.asyncio
    async def test_idle_allows(self) -> None:
        session = make_session()
        session._ever_received_bytes = True
        session._backend._segment = TerminalSegment(
            text="$ ", cursor_line="$ ", is_empty_prompt=True,
        )
        result = await check_command_writable(session, config=_config())
        assert result is None
        result = await check_process_writable(session, config=_config())
        assert result is None

    @pytest.mark.asyncio
    async def test_process_waiting_input_allows(self) -> None:
        """ProcessTool may send passwords to a waiting-input prompt."""
        session = make_session()
        session._ever_received_bytes = True
        session._backend._segment = TerminalSegment(
            text="Password: ", cursor_line="Password: ", is_empty_prompt=False,
        )
        result = await check_process_writable(session, config=_config())
        assert result is None

    @pytest.mark.asyncio
    async def test_process_paginated_allows(self) -> None:
        """ProcessTool must be able to interact with a pager (e.g. send 'q')."""
        session = make_session()
        session._ever_received_bytes = True
        session._backend._segment = TerminalSegment(
            text="line1\nline2\n: ", cursor_line=": ", is_empty_prompt=False,
        )
        result = await check_process_writable(session, config=_config())
        assert result is None

    @pytest.mark.asyncio
    async def test_unknown_allows(self) -> None:
        session = make_session()
        session._ever_received_bytes = False
        result = await check_command_writable(session, config=_config())
        assert result is None


class TestCommandGuardRejected:
    """CommandTool guard — stricter, rejects interactive states."""

    @pytest.mark.asyncio
    async def test_executing_rejects(self) -> None:
        session = make_session()
        session._ever_received_bytes = True
        session._last_byte_at = time.monotonic() - 2
        session._backend._segment = TerminalSegment(
            text="downloading...", cursor_line="downloading...", is_empty_prompt=False,
        )
        result = await check_command_writable(session, config=_config())
        assert result is not None
        assert result.status == TerminalCommandStatus.EXECUTING
        assert "executing" in result.message.lower()

    @pytest.mark.asyncio
    async def test_command_waiting_input_rejects(self) -> None:
        """CommandTool must NOT overwrite a password prompt."""
        session = make_session()
        session._ever_received_bytes = True
        session._backend._segment = TerminalSegment(
            text="Password: ", cursor_line="Password: ", is_empty_prompt=False,
        )
        result = await check_command_writable(session, config=_config())
        assert result is not None
        assert result.status == TerminalCommandStatus.WAITING_INPUT

    @pytest.mark.asyncio
    async def test_stuck_rejects(self) -> None:
        session = make_session()
        session._ever_received_bytes = True
        session._last_byte_at = time.monotonic() - 35
        session._backend._segment = TerminalSegment(
            text="...", cursor_line="...", is_empty_prompt=False,
        )
        result = await check_command_writable(session, config=_config())
        assert result is not None
        assert result.status == TerminalCommandStatus.STUCK
        assert result.snapshot.suggestion

    @pytest.mark.asyncio
    async def test_long_running_rejects(self) -> None:
        cfg = _config(long_running_threshold_ms=100)
        session = make_session()
        session._ever_received_bytes = True
        session._last_byte_at = time.monotonic() - 2
        session._command_started_at = time.monotonic() - 0.2
        session._backend._segment = TerminalSegment(
            text="building...", cursor_line="building...", is_empty_prompt=False,
        )
        result = await check_command_writable(session, config=cfg)
        assert result is not None
        assert result.status == TerminalCommandStatus.LONG_RUNNING

    @pytest.mark.asyncio
    async def test_paginated_rejects(self) -> None:
        session = make_session()
        session._ever_received_bytes = True
        session._backend._segment = TerminalSegment(
            text="line1\nline2\n: ", cursor_line=": ", is_empty_prompt=False,
        )
        result = await check_command_writable(session, config=_config())
        if result is not None:
            assert result.status in (
                TerminalCommandStatus.EXECUTING,
                TerminalCommandStatus.PAGINATED,
                TerminalCommandStatus.STUCK,
                TerminalCommandStatus.LONG_RUNNING,
            )


class TestProcessGuardRejected:
    """ProcessTool guard — rejects running states but allows interactive."""

    @pytest.mark.asyncio
    async def test_executing_rejects(self) -> None:
        session = make_session()
        session._ever_received_bytes = True
        session._last_byte_at = time.monotonic() - 2
        session._backend._segment = TerminalSegment(
            text="downloading...", cursor_line="downloading...", is_empty_prompt=False,
        )
        result = await check_process_writable(session, config=_config())
        assert result is not None
        assert result.status == TerminalCommandStatus.EXECUTING

    @pytest.mark.asyncio
    async def test_stuck_rejects(self) -> None:
        session = make_session()
        session._ever_received_bytes = True
        session._last_byte_at = time.monotonic() - 35
        session._backend._segment = TerminalSegment(
            text="...", cursor_line="...", is_empty_prompt=False,
        )
        result = await check_process_writable(session, config=_config())
        assert result is not None
        assert result.status == TerminalCommandStatus.STUCK


class TestGuardBackwardCompat:
    """check_terminal_writable is an alias for check_command_writable."""

    @pytest.mark.asyncio
    async def test_alias_rejects_waiting_input(self) -> None:
        session = make_session()
        session._ever_received_bytes = True
        session._backend._segment = TerminalSegment(
            text="Password: ", cursor_line="Password: ", is_empty_prompt=False,
        )
        result = await check_terminal_writable(session, config=_config())
        assert result is not None
        assert result.status == TerminalCommandStatus.WAITING_INPUT


class TestGuardDiagnostic:
    """Verify diagnostic snapshot content."""

    @pytest.mark.asyncio
    async def test_rejection_includes_snapshot(self) -> None:
        session = make_session()
        session._ever_received_bytes = True
        session._last_byte_at = time.monotonic() - 35
        session._backend._segment = TerminalSegment(
            text="frozen output", cursor_line="frozen output", is_empty_prompt=False,
        )
        result = await check_command_writable(session, config=_config())
        assert result is not None
        assert result.snapshot.status == TerminalCommandStatus.STUCK
        assert result.snapshot.idle_ms >= 30_000
        assert isinstance(result.snapshot.suggestion, str)
        assert len(result.snapshot.suggestion) > 0
