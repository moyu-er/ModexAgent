from __future__ import annotations

import asyncio
from collections.abc import Callable
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest

from modex_agent.core.tool_manager import Tool
from modex_agent.tools.terminal import subprocess_tool
from modex_agent.tools.terminal._persistent_session import (
    PersistentShellManager,
    PersistentShellSession,
    _PendingWait,
    _Phase,
)
from modex_agent.tools.terminal.command_tool import CommandTool
from modex_agent.tools.terminal.managers import TerminalManagerBase
from modex_agent.tools.terminal.persistent_bash import BashInputTool, PersistentBashTool
from modex_agent.tools.terminal.process_registry import ProcessRegistry
from modex_agent.tools.terminal.process_tool import ProcessTool
from modex_agent.tools.terminal.subprocess_tool import SubprocessExecutor, SubprocessTool
from modex_agent.tools.terminal.tool import TerminalTool
from modex_agent.tools.terminal.types import ProcessStatus


class _AlivePersistentProcess:
    def isalive(self) -> bool:
        return True


class _FakePersistentSession(PersistentShellSession):
    def __init__(self) -> None:
        super().__init__(shell_argv=["/bin/bash"])
        self._proc = _AlivePersistentProcess()
        self.collect_started = asyncio.Event()
        self.collect_count = 0
        self.sent: list[str] = []
        self.drained = False
        self.terminated = False

    async def _ensure_started(self) -> None:
        return

    async def _drain_stale(self) -> None:
        return

    async def _send(self, data: str) -> None:
        self.sent.append(data)

    async def _collect(self, pending: _PendingWait) -> str:
        self.collect_count += 1
        if self.collect_count == 1:
            self.collect_started.set()
            await asyncio.Event().wait()
        self._pending = None
        self._phase = _Phase.IDLE
        self.drained = True
        return "interrupted"

    def _terminate_session_sync(self) -> None:
        self.terminated = True
        self._proc = None
        self._pending = None
        self._phase = _Phase.IDLE


class _FakePersistentManager(PersistentShellManager):
    def __init__(self, session: PersistentShellSession) -> None:
        self._session = session

    def session_for(self, session_id: str | None) -> PersistentShellSession:
        return self._session


class _FakeProcess(asyncio.subprocess.Process):
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self._fake_returncode: int | None = None

    @property
    def returncode(self) -> int | None:
        return self._fake_returncode

    async def communicate(
        self,
        input: bytes | bytearray | memoryview[int] | None = None,
    ) -> tuple[bytes, bytes]:
        self.started.set()
        await self.release.wait()
        self._fake_returncode = 0
        return b"", b""


class _FakeSubprocessExecutor(SubprocessExecutor):
    def __init__(self, *processes: asyncio.subprocess.Process) -> None:
        super().__init__(shell_info=subprocess_tool._default_fallback_shell())
        self._fake_processes = iter(processes)

    async def _spawn(
        self,
        command: str,
        cwd: str,
        env: dict[str, str],
    ) -> asyncio.subprocess.Process:
        return next(self._fake_processes)


@pytest.mark.parametrize(
    "build_tool",
    [
        lambda manager: PersistentBashTool(manager=manager),
        BashInputTool,
    ],
)
@pytest.mark.asyncio
async def test_persistent_tool_recovers_bound_session_when_cancelled(
    build_tool: Callable[[PersistentShellManager], Tool],
) -> None:
    # Given
    session = _FakePersistentSession()
    tool = build_tool(_FakePersistentManager(session))
    execution = asyncio.create_task(session.run_command("long-running"))
    await session.collect_started.wait()
    execution.cancel()
    with pytest.raises(asyncio.CancelledError):
        await execution

    # When
    await tool.on_cancel()

    # Then
    assert session.sent[-1] == "\x03"
    assert session.drained is True
    assert session._phase is _Phase.IDLE
    assert session._proc is not None
    assert session.terminated is False


@pytest.mark.asyncio
async def test_subprocess_execute_terminates_its_process_tree_when_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    process = _FakeProcess()
    tool = SubprocessTool(executor=_FakeSubprocessExecutor(process))
    terminated: list[asyncio.subprocess.Process] = []

    async def record_termination(active: asyncio.subprocess.Process) -> None:
        terminated.append(active)

    monkeypatch.setattr(
        subprocess_tool,
        "terminate_process_group",
        record_termination,
        raising=False,
    )
    execution = asyncio.create_task(tool.execute("long-running"))
    await process.started.wait()
    execution.cancel()
    with pytest.raises(asyncio.CancelledError):
        await execution

    # Then
    assert terminated == [process]


@pytest.mark.asyncio
async def test_subprocess_concurrent_cancel_terminates_only_its_own_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    process_a = _FakeProcess()
    process_b = _FakeProcess()
    tool = SubprocessTool(executor=_FakeSubprocessExecutor(process_a, process_b))
    terminated: list[asyncio.subprocess.Process] = []

    async def record_termination(process: asyncio.subprocess.Process) -> None:
        terminated.append(process)

    monkeypatch.setattr(
        subprocess_tool,
        "terminate_process_group",
        record_termination,
        raising=False,
    )
    execution_a = asyncio.create_task(tool.execute("long-running-a"))
    await process_a.started.wait()
    execution_b = asyncio.create_task(tool.execute("short-running-b"))
    await process_b.started.wait()

    # When
    execution_a.cancel()
    with pytest.raises(asyncio.CancelledError):
        await execution_a

    # Then
    assert terminated == [process_a]
    process_b.release.set()
    assert await execution_b == "(no output)"


@pytest.mark.parametrize("tool_type", [CommandTool, ProcessTool])
@pytest.mark.asyncio
async def test_terminal_command_tools_interrupt_running_command_on_cancel(
    tool_type: type[Tool],
) -> None:
    session = SimpleNamespace(
        name="default",
        interrupt=AsyncMock(),
        refresh_output=AsyncMock(),
        last_command_output=AsyncMock(return_value=""),
        current_segment=AsyncMock(
            return_value=SimpleNamespace(is_empty_prompt=True, cursor_line="")
        ),
    )
    manager = cast(
        TerminalManagerBase,
        SimpleNamespace(get_default=AsyncMock(return_value=session)),
    )
    registry = ProcessRegistry()
    running = registry.create(
        command="long-running",
        terminal="default",
        cwd=None,
        pid=None,
    )
    tool = (
        CommandTool(manager, registry)
        if tool_type is CommandTool
        else ProcessTool(registry, manager)
    )

    await tool.on_cancel()

    session.interrupt.assert_awaited_once()
    session.refresh_output.assert_awaited_once()
    finished = registry.get_finished(running.id)
    assert finished is not None
    assert finished.status is ProcessStatus.KILLED


@pytest.mark.asyncio
async def test_terminal_management_keeps_default_cancel_hook() -> None:
    tool = TerminalTool.__new__(TerminalTool)

    await tool.on_cancel()

    assert TerminalTool.on_cancel is Tool.on_cancel
