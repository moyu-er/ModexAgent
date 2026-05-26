"""Backend contract tests for the stream-based protocol.

Verifies that TerminalBackend subclasses correctly implement the new
stream operations: read_pending, current_segment, interrupt, stdin_writable,
plus platform and visibility properties.
"""

from __future__ import annotations

import pytest

from framework.tools.terminal.backends.base import TerminalBackend
from framework.tools.terminal.results import TerminalRead, TerminalSegment
from framework.tools.terminal.types import Platform, TerminalVisibility


class FakeBackend(TerminalBackend):
    platform = Platform.WINDOWS
    visibility = TerminalVisibility.HIDDEN

    def __init__(self) -> None:
        self.started = False
        self.writes: list[str] = []

    async def start(self, shell, cwd, env) -> None:
        self.started = True

    async def write(self, data: str) -> None:
        self.writes.append(data)

    async def read_pending(self, timeout: float, max_size: int) -> TerminalRead:
        return TerminalRead(stdout="out", raw="out")

    async def current_segment(self) -> TerminalSegment:
        return TerminalSegment(text="$ ", cursor_line="$ ", is_empty_prompt=True)

    async def interrupt(self) -> None:
        self.writes.append("\x03")

    async def terminate(self) -> None:
        self.started = False

    async def kill(self) -> None:
        self.started = False

    async def is_alive(self) -> bool:
        return self.started

    def stdin_writable(self) -> bool:
        return self.started

    # Old protocol stubs (kept for backward compatibility)
    async def drain_startup(self) -> None:
        pass

    async def clear_input_line(self) -> None:
        pass


@pytest.mark.asyncio
async def test_backend_protocol_supports_stream_operations() -> None:
    backend = FakeBackend()

    await backend.start(shell=None, cwd=None, env=None)
    await backend.write("echo hi\r")
    read = await backend.read_pending(timeout=0.1, max_size=100)
    segment = await backend.current_segment()
    await backend.interrupt()

    assert await backend.is_alive() is True
    assert backend.stdin_writable() is True
    assert read.stdout == "out"
    assert segment.is_empty_prompt is True
    assert backend.writes == ["echo hi\r", "\x03"]


def test_backend_has_platform_property() -> None:
    backend = FakeBackend()
    assert backend.platform is Platform.WINDOWS


def test_backend_has_visibility_property() -> None:
    backend = FakeBackend()
    assert backend.visibility is TerminalVisibility.HIDDEN
