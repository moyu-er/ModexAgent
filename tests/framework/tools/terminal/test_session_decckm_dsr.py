"""Tests for DECCKM tracking and DSR auto-response in TerminalSession.poll_once()."""

from __future__ import annotations

import pytest

from framework.tools.terminal.results import TerminalRead, TerminalSegment
from framework.tools.terminal.session import TerminalSession
from framework.tools.terminal.pty_keys import CursorKeyMode
from framework.tools.terminal.types import Platform, ShellFamily, ShellInfo


SMKX = b"\x1b[?1h"
RMKX = b"\x1b[?1l"
DSR = b"\x1b[6n"


class FakeBackend:
    """Minimal backend stub for testing poll_once() processing."""

    platform = Platform.WINDOWS
    visibility = None  # type: ignore[assignment]

    def __init__(self) -> None:
        self._alive = True
        self._output_queue: list[TerminalRead] = []
        self._writes: list[str] = []

    async def start(self, shell, cwd, env) -> None:
        pass

    async def write(self, data: str) -> None:
        self._writes.append(data)

    async def read_pending(self, timeout: float = 5.0, max_size: int = 65536) -> TerminalRead:
        if self._output_queue:
            return self._output_queue.pop(0)
        return TerminalRead()

    async def current_segment(self) -> TerminalSegment:
        return TerminalSegment(text="$ ")

    async def interrupt(self) -> None:
        pass

    async def terminate(self) -> None:
        self._alive = False

    async def kill(self) -> None:
        self._alive = False

    async def is_alive(self) -> bool:
        return self._alive

    def stdin_writable(self) -> bool:
        return self._alive

    async def drain_startup(self) -> None:
        pass

    async def clear_input_line(self) -> None:
        pass

    async def read(self, timeout: float, max_size: int) -> str:
        r = await self.read_pending(timeout, max_size)
        return r.raw


# ---------------------------------------------------------------------------
# DECCKM detection tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_decckm_detects_application_mode() -> None:
    backend = FakeBackend()
    session = TerminalSession(
        name="test",
        backend=backend,
        shell_info=ShellInfo(ShellFamily.BASH, "bash", Platform.WINDOWS),
    )
    # Simulate vim starting — sends smkx (application mode)
    smkx_str = SMKX.decode("utf-8", "replace")
    backend._output_queue = [TerminalRead(stdout=smkx_str, raw=smkx_str)]

    await session.poll_once()

    assert session.cursor_key_mode is CursorKeyMode.APPLICATION


@pytest.mark.asyncio
async def test_decckm_detects_normal_mode() -> None:
    backend = FakeBackend()
    session = TerminalSession(
        name="test",
        backend=backend,
        shell_info=ShellInfo(ShellFamily.BASH, "bash", Platform.WINDOWS),
    )
    session.cursor_key_mode = CursorKeyMode.APPLICATION
    # Simulate vim exiting — sends rmkx (normal mode)
    rmkx_str = RMKX.decode("utf-8", "replace")
    backend._output_queue = [TerminalRead(stdout=rmkx_str, raw=rmkx_str)]

    await session.poll_once()

    assert session.cursor_key_mode is CursorKeyMode.NORMAL


# ---------------------------------------------------------------------------
# DSR auto-response tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dsr_auto_response() -> None:
    backend = FakeBackend()
    session = TerminalSession(
        name="test",
        backend=backend,
        shell_info=ShellInfo(ShellFamily.BASH, "bash", Platform.WINDOWS),
    )
    # Simulate TUI program querying cursor position
    dsr_str = DSR.decode("utf-8", "replace")
    backend._output_queue = [TerminalRead(stdout=dsr_str, raw=dsr_str)]

    await session.poll_once()

    # Should have auto-responded with cursor position
    assert any("\x1b[1;1R" in w for w in backend._writes)


@pytest.mark.asyncio
async def test_dsr_no_response_when_stdin_not_writable() -> None:
    backend = FakeBackend()
    session = TerminalSession(
        name="test",
        backend=backend,
        shell_info=ShellInfo(ShellFamily.BASH, "bash", Platform.WINDOWS),
    )
    backend._alive = False  # stdin not writable
    dsr_str = DSR.decode("utf-8", "replace")
    backend._output_queue = [TerminalRead(stdout=dsr_str, raw=dsr_str)]

    await session.poll_once()

    # Should NOT have responded
    assert not any("\x1b[1;1R" in w for w in backend._writes)


# ---------------------------------------------------------------------------
# Output stripping tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_decckm_strips_sequences_from_output() -> None:
    backend = FakeBackend()
    session = TerminalSession(
        name="test",
        backend=backend,
        shell_info=ShellInfo(ShellFamily.BASH, "bash", Platform.WINDOWS),
    )
    # Output with smkx mixed in
    text = "hello" + SMKX.decode("utf-8", "replace") + " world"
    backend._output_queue = [TerminalRead(stdout=text, raw=text)]

    result = await session.poll_once()

    # smkx should be stripped from output
    assert "\x1b[?1h" not in result.stdout
    assert "hello" in result.stdout
    assert "world" in result.stdout


@pytest.mark.asyncio
async def test_dsr_stripped_from_output() -> None:
    backend = FakeBackend()
    session = TerminalSession(
        name="test",
        backend=backend,
        shell_info=ShellInfo(ShellFamily.BASH, "bash", Platform.WINDOWS),
    )
    text = "before" + DSR.decode("utf-8", "replace") + "after"
    backend._output_queue = [TerminalRead(stdout=text, raw=text)]

    result = await session.poll_once()

    # DSR query should be stripped from output
    assert "\x1b[6n" not in result.stdout
    assert "before" in result.stdout
    assert "after" in result.stdout


# ---------------------------------------------------------------------------
# No-op when no control sequences present
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_passthrough_plain_output() -> None:
    backend = FakeBackend()
    session = TerminalSession(
        name="test",
        backend=backend,
        shell_info=ShellInfo(ShellFamily.BASH, "bash", Platform.WINDOWS),
    )
    backend._output_queue = [TerminalRead(stdout="plain text", raw="plain text")]

    result = await session.poll_once()

    assert result.stdout == "plain text"
    assert result.raw == "plain text"


@pytest.mark.asyncio
async def test_empty_read_unchanged() -> None:
    backend = FakeBackend()
    session = TerminalSession(
        name="test",
        backend=backend,
        shell_info=ShellInfo(ShellFamily.BASH, "bash", Platform.WINDOWS),
    )
    # No output queued — returns empty TerminalRead

    result = await session.poll_once()

    assert result.stdout == ""
    assert session.cursor_key_mode is CursorKeyMode.UNKNOWN
