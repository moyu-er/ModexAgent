from __future__ import annotations

import pytest

from framework.tools.terminal.process_registry import ProcessRegistry
from framework.tools.terminal.process_tool import ProcessTool
from framework.tools.terminal.types import ProcessStatus


class FakeTerminal:
    def __init__(self) -> None:
        self.writes: list[str] = []
        self.interrupted = False
        self.killed = False
        self.name = "default"

    async def write(self, data: str) -> None:
        self.writes.append(data)

    async def interrupt(self) -> None:
        self.interrupted = True

    async def terminate(self) -> None:
        self.killed = True

    async def is_alive(self) -> bool:
        return not self.killed


class FakeManager:
    def __init__(self, terminal: FakeTerminal) -> None:
        self.terminal = terminal

    async def get_or_create(self, name, workdir=None):
        return self.terminal


@pytest.mark.asyncio
async def test_process_poll_drains_pending_once() -> None:
    registry = ProcessRegistry()
    session = registry.create(command="server", terminal="default", cwd=None, pid=1)
    registry.append_output(session.id, "stdout", "ready\n")
    tool = ProcessTool(registry=registry, manager=FakeManager(FakeTerminal()))

    first = await tool.execute(action="poll", session_id=session.id)
    second = await tool.execute(action="poll", session_id=session.id)

    assert "ready" in first
    assert "(no new output)" in second


@pytest.mark.asyncio
async def test_process_write_submit_interrupt_and_kill() -> None:
    registry = ProcessRegistry()
    session = registry.create(command="ssh host", terminal="default", cwd=None, pid=2)
    terminal = FakeTerminal()
    tool = ProcessTool(registry=registry, manager=FakeManager(terminal))

    await tool.execute(action="write", session_id=session.id, data="password")
    await tool.execute(action="submit", session_id=session.id)
    await tool.execute(action="interrupt", session_id=session.id)
    await tool.execute(action="kill", session_id=session.id)

    assert terminal.writes == ["password", "\r"]
    assert terminal.interrupted is True
    assert terminal.killed is True
    assert registry.get_finished(session.id).status is ProcessStatus.KILLED


@pytest.mark.asyncio
async def test_process_log_reads_aggregated_output() -> None:
    registry = ProcessRegistry()
    session = registry.create(command="build", terminal="default", cwd=None, pid=3)
    registry.append_output(session.id, "stdout", "line1\nline2\nline3\n")
    tool = ProcessTool(registry=registry, manager=FakeManager(FakeTerminal()))

    text = await tool.execute(action="log", session_id=session.id, offset=1, limit=1)

    assert "line2" in text
    assert "line1" not in text


@pytest.mark.asyncio
async def test_process_send_keys_encodes_named_keys() -> None:
    registry = ProcessRegistry()
    session = registry.create(command="less file", terminal="default", cwd=None, pid=4)
    terminal = FakeTerminal()
    tool = ProcessTool(registry=registry, manager=FakeManager(terminal))

    await tool.execute(action="send_keys", session_id=session.id, keys=["escape", "enter", "c-c"])

    assert terminal.writes == ["\x1b\r\x03"]


@pytest.mark.asyncio
async def test_process_paste_writes_text() -> None:
    registry = ProcessRegistry()
    session = registry.create(command="python", terminal="default", cwd=None, pid=5)
    terminal = FakeTerminal()
    tool = ProcessTool(registry=registry, manager=FakeManager(terminal))

    await tool.execute(action="paste", session_id=session.id, text="print('hi')", bracketed=False)

    assert terminal.writes == ["print('hi')"]


@pytest.mark.asyncio
async def test_process_list_shows_sessions() -> None:
    registry = ProcessRegistry()
    registry.create(command="build", terminal="default", cwd=None, pid=10)
    registry.create(command="test", terminal="default", cwd=None, pid=11)
    tool = ProcessTool(registry=registry, manager=FakeManager(FakeTerminal()))

    text = await tool.execute(action="list")

    assert "build" in text
    assert "test" in text


@pytest.mark.asyncio
async def test_process_clear_removes_finished() -> None:
    registry = ProcessRegistry()
    session = registry.create(command="done", terminal="default", cwd=None, pid=20)
    registry.mark_exited(session.id, exit_code=0, exit_signal=None, status=ProcessStatus.COMPLETED)
    tool = ProcessTool(registry=registry, manager=FakeManager(FakeTerminal()))

    await tool.execute(action="clear", session_id=session.id)

    assert registry.get_finished(session.id) is None
