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

    async def poll_once(self, timeout: float = 0.1, max_size: int = 65536):
        from framework.tools.terminal.results import TerminalRead
        return TerminalRead()


class FakeManager:
    def __init__(self, terminal: FakeTerminal) -> None:
        self.terminal = terminal

    async def get_default(self):
        return self.terminal


def _assert_xml_result(text: str, action: str) -> None:
    """Assert the result is wrapped in <process_result> XML with the given action."""
    assert "<process_result>" in text
    assert f"<action>{action}</action>" in text
    assert "</process_result>" in text


@pytest.mark.asyncio
async def test_process_write_submit_interrupt_and_kill() -> None:
    registry = ProcessRegistry()
    registry.create(command="ssh host", terminal="default", cwd=None, pid=2)
    terminal = FakeTerminal()
    tool = ProcessTool(registry=registry, manager=FakeManager(terminal))

    write_result = await tool.execute(action="write", data="password", submit=False)
    _assert_xml_result(write_result, "write")

    await tool.execute(action="submit")
    await tool.execute(action="interrupt")
    kill_result = await tool.execute(action="kill")
    _assert_xml_result(kill_result, "kill")

    assert terminal.writes == ["password", "\r"]
    assert terminal.interrupted is True
    assert terminal.killed is True
    running = registry.get_running_by_terminal("default")
    assert running is None
    finished = registry.get_finished_by_terminal("default")
    assert finished is not None
    assert finished.status is ProcessStatus.KILLED


@pytest.mark.asyncio
async def test_process_send_keys_encodes_named_keys() -> None:
    registry = ProcessRegistry()
    registry.create(command="less file", terminal="default", cwd=None, pid=4)
    terminal = FakeTerminal()
    tool = ProcessTool(registry=registry, manager=FakeManager(terminal))

    text = await tool.execute(action="send_keys", keys=["escape", "enter", "c-c"])

    _assert_xml_result(text, "send_keys")
    assert terminal.writes == ["\x1b\r\x03"]


@pytest.mark.asyncio
async def test_process_paste_writes_text() -> None:
    registry = ProcessRegistry()
    registry.create(command="python", terminal="default", cwd=None, pid=5)
    terminal = FakeTerminal()
    tool = ProcessTool(registry=registry, manager=FakeManager(terminal))

    text = await tool.execute(action="paste", text="print('hi')")

    _assert_xml_result(text, "paste")
    assert terminal.writes == ["print('hi')"]


@pytest.mark.asyncio
async def test_process_paste_with_bracketed_mode() -> None:
    registry = ProcessRegistry()
    registry.create(command="bash", terminal="default", cwd=None, pid=6)
    terminal = FakeTerminal()
    terminal.bracketed_paste_enabled = True
    tool = ProcessTool(registry=registry, manager=FakeManager(terminal))

    text = await tool.execute(action="paste", text="hello")

    _assert_xml_result(text, "paste")
    # Should be wrapped with bracketed paste start/end
    assert terminal.writes == ["\x1b[200~hello\x1b[201~"]


@pytest.mark.asyncio
async def test_process_clear_removes_finished() -> None:
    registry = ProcessRegistry()
    session = registry.create(command="done", terminal="default", cwd=None, pid=20)
    registry.mark_exited(session.id, exit_code=0, exit_signal=None, status=ProcessStatus.COMPLETED)
    tool = ProcessTool(registry=registry, manager=FakeManager(FakeTerminal()))

    text = await tool.execute(action="clear")

    _assert_xml_result(text, "clear")
    assert registry.get_finished(session.id) is None


@pytest.mark.asyncio
async def test_process_remove_running() -> None:
    registry = ProcessRegistry()
    session = registry.create(command="long", terminal="default", cwd=None, pid=40)
    terminal = FakeTerminal()
    tool = ProcessTool(registry=registry, manager=FakeManager(terminal))

    text = await tool.execute(action="remove")

    _assert_xml_result(text, "remove")
    assert "Killed and removed" in text
    assert registry.get_running_by_terminal("default") is None


@pytest.mark.asyncio
async def test_process_remove_finished() -> None:
    registry = ProcessRegistry()
    session = registry.create(command="done", terminal="default", cwd=None, pid=41)
    registry.mark_exited(session.id, exit_code=0, exit_signal=None, status=ProcessStatus.COMPLETED)
    tool = ProcessTool(registry=registry, manager=FakeManager(FakeTerminal()))

    text = await tool.execute(action="remove")

    _assert_xml_result(text, "remove")
    assert "Removed finished" in text
    assert registry.get_finished_by_terminal("default") is None
