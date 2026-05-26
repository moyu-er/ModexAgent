from __future__ import annotations

import pytest

from framework.tools.terminal.config import TerminalRuntimeConfig
from framework.tools.terminal.managers import BaseTerminalManager
from framework.tools.terminal.results import TerminalRead, TerminalSegment
from framework.tools.terminal.types import Platform, ShellFamily, ShellInfo, TerminalVisibility


class FakeBackend:
    platform = Platform.WINDOWS
    visibility = TerminalVisibility.HIDDEN

    def __init__(self) -> None:
        self.started = False
        self.writes: list[str] = []

    async def start(self, shell, cwd, env) -> None:
        self.started = True

    async def drain_startup(self) -> None:
        pass

    async def clear_input_line(self) -> None:
        pass

    async def write(self, data: str) -> None:
        self.writes.append(data)

    async def read_pending(self, timeout: float, max_size: int) -> TerminalRead:
        return TerminalRead(stdout="ready\n", raw="ready\n")

    async def read(self, timeout: float = 5.0, max_size: int = 65536) -> str:
        return ""

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


@pytest.mark.asyncio
async def test_manager_creates_default_session_without_tool_knowing_visibility() -> None:
    manager = BaseTerminalManager(
        shell_info=ShellInfo(ShellFamily.CMD, "cmd.exe", Platform.WINDOWS),
        visibility=TerminalVisibility.HIDDEN,
        backend_factory=FakeBackend,
        config=TerminalRuntimeConfig(),
    )

    session = await manager.get_or_create(None)
    default = await manager.get_default()

    assert session.name == "default"
    assert default is session
    assert manager.visibility is TerminalVisibility.HIDDEN


@pytest.mark.asyncio
async def test_terminal_session_start_write_poll_and_current_segment() -> None:
    manager = BaseTerminalManager(
        shell_info=ShellInfo(ShellFamily.CMD, "cmd.exe", Platform.WINDOWS),
        visibility=TerminalVisibility.HIDDEN,
        backend_factory=FakeBackend,
        config=TerminalRuntimeConfig(),
    )
    session = await manager.get_or_create("build")

    await session.ensure_started()
    await session.write("npm test\r")
    read = await session.poll_once()
    segment = await session.current_segment()

    assert read.stdout == "ready\n"
    assert segment.text == "$ "


@pytest.mark.asyncio
async def test_manager_select_and_close() -> None:
    manager = BaseTerminalManager(
        shell_info=ShellInfo(ShellFamily.CMD, "cmd.exe", Platform.WINDOWS),
        visibility=TerminalVisibility.HIDDEN,
        backend_factory=FakeBackend,
        config=TerminalRuntimeConfig(),
    )
    await manager.get_or_create("one")
    await manager.get_or_create("two")

    await manager.select_default("two")
    closed = await manager.close("two")
    default = await manager.get_default()

    assert closed is True
    assert default.name == "one"
