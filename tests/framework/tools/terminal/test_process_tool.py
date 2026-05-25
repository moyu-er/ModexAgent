from __future__ import annotations

import pytest

from framework.tools.terminal.process_registry import ProcessRegistry
from framework.tools.terminal.process_tool import ProcessTool
from framework.tools.terminal.types import ProcessStatus


class FakeTerminal:
    def __init__(self, name: str = "default") -> None:
        self.writes: list[str] = []
        self.interrupted = False
        self.killed = False
        self.name = name
        self.cursor_key_mode = "unknown"
        self.bracketed_paste_enabled = False
        self._segment = None

    async def write(self, data: str) -> None:
        self.writes.append(data)

    async def interrupt(self) -> None:
        self.interrupted = True

    async def terminate(self) -> None:
        self.killed = True

    async def is_alive(self) -> bool:
        return not self.killed

    async def current_segment(self):
        return self._segment


class FakeManager:
    def __init__(self, terminal: FakeTerminal) -> None:
        self.terminal = terminal

    async def get_default_session(self):
        return self.terminal


@pytest.mark.asyncio
async def test_process_poll_drains_pending_once() -> None:
    registry = ProcessRegistry()
    session = registry.create(command="server", terminal="default", cwd=None, pid=1)
    registry.append_output(session.id, "stdout", "ready\n")
    terminal = FakeTerminal()
    tool = ProcessTool(registry=registry, manager=FakeManager(terminal))

    first = await tool.execute(action="poll")
    second = await tool.execute(action="poll")

    assert "ready" in first
    assert "(no new output)" in second



@pytest.mark.asyncio
async def test_process_write_submit_interrupt_and_kill() -> None:
    registry = ProcessRegistry()
    registry.create(command="ssh host", terminal="default", cwd=None, pid=2)
    terminal = FakeTerminal()
    tool = ProcessTool(registry=registry, manager=FakeManager(terminal))

    await tool.execute(action="write", data="password")
    await tool.execute(action="submit")
    await tool.execute(action="interrupt")
    await tool.execute(action="kill")

    assert terminal.writes == ["password", "\r"]
    assert terminal.interrupted is True
    assert terminal.killed is True
    running = registry.get_running_by_terminal("default")
    assert running is None
    finished = registry.get_finished_by_terminal("default")
    assert finished is not None
    assert finished.status is ProcessStatus.KILLED


@pytest.mark.asyncio
async def test_process_log_reads_aggregated_output() -> None:
    registry = ProcessRegistry()
    session = registry.create(command="build", terminal="default", cwd=None, pid=3)
    registry.append_output(session.id, "stdout", "line1\nline2\nline3\n")
    terminal = FakeTerminal()
    tool = ProcessTool(registry=registry, manager=FakeManager(terminal))

    text = await tool.execute(action="log", offset=1, limit=1)

    assert "line2" in text
    assert "line1" not in text


@pytest.mark.asyncio
async def test_process_send_keys_encodes_named_keys() -> None:
    registry = ProcessRegistry()
    registry.create(command="less file", terminal="default", cwd=None, pid=4)
    terminal = FakeTerminal()
    tool = ProcessTool(registry=registry, manager=FakeManager(terminal))

    await tool.execute(action="send_keys", keys=["escape", "enter", "c-c"])

    assert terminal.writes == ["\x1b\r\x03"]


@pytest.mark.asyncio
async def test_process_paste_writes_text() -> None:
    registry = ProcessRegistry()
    registry.create(command="python", terminal="default", cwd=None, pid=5)
    terminal = FakeTerminal()
    tool = ProcessTool(registry=registry, manager=FakeManager(terminal))

    await tool.execute(action="paste", text="print('hi')")

    assert terminal.writes == ["print('hi')"]


@pytest.mark.asyncio
async def test_process_paste_with_bracketed_mode() -> None:
    registry = ProcessRegistry()
    registry.create(command="bash", terminal="default", cwd=None, pid=6)
    terminal = FakeTerminal()
    terminal.bracketed_paste_enabled = True
    tool = ProcessTool(registry=registry, manager=FakeManager(terminal))

    await tool.execute(action="paste", text="hello")

    # Should be wrapped with bracketed paste start/end
    assert terminal.writes == ["\x1b[200~hello\x1b[201~"]


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

    await tool.execute(action="clear")

    assert registry.get_finished(session.id) is None


@pytest.mark.asyncio
async def test_process_poll_finished_session() -> None:
    registry = ProcessRegistry()
    session = registry.create(command="echo hi", terminal="default", cwd=None, pid=30)
    registry.append_output(session.id, "stdout", "hello\n")
    registry.mark_exited(session.id, exit_code=0, exit_signal=None, status=ProcessStatus.COMPLETED)
    tool = ProcessTool(registry=registry, manager=FakeManager(FakeTerminal()))

    text = await tool.execute(action="poll")

    assert "hello" in text
    assert "exited" in text


@pytest.mark.asyncio
async def test_process_remove_running() -> None:
    registry = ProcessRegistry()
    session = registry.create(command="long", terminal="default", cwd=None, pid=40)
    terminal = FakeTerminal()
    tool = ProcessTool(registry=registry, manager=FakeManager(terminal))

    text = await tool.execute(action="remove")

    assert "Killed and removed" in text
    assert registry.get_running_by_terminal("default") is None


@pytest.mark.asyncio
async def test_process_remove_finished() -> None:
    registry = ProcessRegistry()
    session = registry.create(command="done", terminal="default", cwd=None, pid=41)
    registry.mark_exited(session.id, exit_code=0, exit_signal=None, status=ProcessStatus.COMPLETED)
    tool = ProcessTool(registry=registry, manager=FakeManager(FakeTerminal()))

    text = await tool.execute(action="remove")

    assert "Removed finished" in text
    assert registry.get_finished_by_terminal("default") is None


@pytest.mark.asyncio
async def test_process_error_no_session() -> None:
    registry = ProcessRegistry()
    tool = ProcessTool(registry=registry, manager=FakeManager(FakeTerminal()))

    text = await tool.execute(action="poll")

    assert "[Error]" in text


@pytest.mark.asyncio
async def test_process_poll_with_tui_screen_snapshot() -> None:
    registry = ProcessRegistry()
    session = registry.create(command="vim file", terminal="default", cwd=None, pid=50)
    registry.append_output(session.id, "stdout", "opened\n")

    terminal = FakeTerminal()
    terminal.cursor_key_mode = "application"
    # Simulate a screen segment
    class FakeSegment:
        text = "~ file contents here\n~ line 2\n"
    terminal._segment = FakeSegment()

    tool = ProcessTool(registry=registry, manager=FakeManager(terminal))
    text = await tool.execute(action="poll")

    assert "opened" in text
    assert "[Screen]" in text
    assert "file contents here" in text
