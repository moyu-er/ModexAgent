import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modex_agent.tools.terminal.backends.base import TerminalBackend
from modex_agent.tools.terminal.command_tool import CommandTool
from modex_agent.tools.terminal.config import TerminalRuntimeConfig
from modex_agent.tools.terminal.managers import BaseTerminalManager, TerminalManagerBase
from modex_agent.tools.terminal.poll_loop import PollOutcome, PollResult
from modex_agent.tools.terminal.process_registry import ProcessRegistry
from modex_agent.tools.terminal.process_tool import ProcessTool
from modex_agent.tools.terminal.pty_keys import normalize_write_payload
from modex_agent.tools.terminal.results import TerminalRead, TerminalSegment
from modex_agent.tools.terminal.session import TerminalSession
from modex_agent.tools.terminal.types import (
    Platform,
    ProcessStatus,
    ShellFamily,
    ShellInfo,
    TerminalVisibility,
)


class _SilentBackend(TerminalBackend):
    def __init__(self) -> None:
        super().__init__()
        self._alive = False

    @property
    def platform(self) -> Platform:
        return Platform.LINUX

    @property
    def visibility(self) -> TerminalVisibility:
        return TerminalVisibility.HIDDEN

    async def start(
        self,
        shell: str | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        del shell, cwd, env
        self._alive = True

    def _shell_family(self) -> ShellFamily:
        return ShellFamily.BASH

    async def write(self, data: str) -> None:
        del data

    async def interrupt(self) -> None:
        return

    async def is_alive(self) -> bool:
        return self._alive

    async def terminate(self) -> None:
        self._alive = False

    async def kill(self) -> None:
        self._alive = False

    def stdin_writable(self) -> bool:
        return True

    async def read_pending(self, timeout: float = 5.0, max_size: int = 65536) -> TerminalRead:
        del timeout, max_size
        return TerminalRead()


@pytest.mark.parametrize(
    ("data", "submit", "expected"),
    [
        ("y\n", True, "y\r"),
        ("y", False, "y"),
        ("y\n", False, "y\n"),
        ("y\r\n", True, "y\r"),
    ],
)
def test_normalize_write_payload(data: str, submit: bool, expected: str) -> None:
    assert normalize_write_payload(data, submit) == expected


def _tool_with_running_session() -> tuple[ProcessTool, ProcessRegistry, MagicMock]:
    registry = ProcessRegistry()
    registry.create(command="read value", terminal="default", cwd=None, pid=None)
    terminal_session = MagicMock(spec=TerminalSession)
    terminal_session.name = "default"
    terminal_session.write = AsyncMock()
    manager = MagicMock(spec=TerminalManagerBase)
    manager.get_default = AsyncMock(return_value=terminal_session)
    return ProcessTool(registry=registry, manager=manager), registry, terminal_session


async def test_process_ctrl_c_token_interrupts_without_typing_literal_text() -> None:
    tool, registry, terminal_session = _tool_with_running_session()
    running = registry.get_running_by_terminal("default")
    assert running is not None
    deadline_before = running.deadline_at
    terminal_session.interrupt = AsyncMock()
    terminal_session.refresh_output = AsyncMock()
    terminal_session.last_command_output = AsyncMock(return_value="interrupted")
    terminal_session.current_segment = AsyncMock(
        return_value=TerminalSegment(text="", is_empty_prompt=True)
    )

    with patch("modex_agent.tools.terminal.process_tool.asyncio.sleep", new=AsyncMock()):
        await tool.execute(data="ctrl+c")

    terminal_session.interrupt.assert_awaited_once_with()
    terminal_session.write.assert_not_awaited()
    assert not registry.list_running()
    assert running.deadline_at > deadline_before


async def test_process_write_submits_exactly_one_enter() -> None:
    tool, registry, terminal_session = _tool_with_running_session()
    running = registry.get_running_by_terminal("default")
    assert running is not None
    deadline_before = running.deadline_at
    result = PollResult(outcome=PollOutcome.INPUT_WAIT, output_parts=[], elapsed_ms=0)
    terminal_session.apply_outcome = MagicMock()

    with (
        patch(
            "modex_agent.tools.terminal.process_tool.check_process_writable",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "modex_agent.tools.terminal.process_tool._drain_terminal_after_action",
            new=AsyncMock(return_value=("continued", result)),
        ),
    ):
        await tool.execute(data="y", submit=True)

    terminal_session.write.assert_awaited_once_with("y\r")
    assert running.deadline_at > deadline_before


async def test_process_write_reports_second_input_advisory() -> None:
    config = TerminalRuntimeConfig(command_deadline_seconds=2, input_wait_idle_ms=0)
    registry = ProcessRegistry(config)
    running = registry.create(
        command="read first; read second",
        terminal="default",
        cwd=None,
        pid=None,
    )
    terminal_session = MagicMock(spec=TerminalSession)
    terminal_session.name = "default"
    terminal_session.write = AsyncMock()
    terminal_session.poll_once = AsyncMock(return_value=TerminalRead(stdout="proceed? "))
    terminal_session.is_alive = AsyncMock(return_value=True)
    terminal_session.current_segment = AsyncMock(return_value=TerminalSegment(text="proceed? "))
    terminal_session._backend = MagicMock()
    terminal_session._backend.stdin_wait_evidence.return_value = False
    terminal_session.apply_outcome = MagicMock()
    manager = MagicMock(spec=TerminalManagerBase)
    manager.get_default = AsyncMock(return_value=terminal_session)

    with patch(
        "modex_agent.tools.terminal.process_tool.check_process_writable",
        new=AsyncMock(return_value=None),
    ):
        result = await ProcessTool(registry, manager, config).execute(data="first")

    assert "<status>waiting_input</status>" in result
    assert "<message>" in result
    assert registry.get_running(running.id) is running
    terminal_session.apply_outcome.assert_called_once()


async def test_process_write_timeout_closes_tab() -> None:
    config = TerminalRuntimeConfig(command_deadline_seconds=2, input_wait_idle_ms=10_000)
    manager = BaseTerminalManager(
        shell_info=ShellInfo(
            family=ShellFamily.BASH,
            path="/bin/bash",
            platform=Platform.LINUX,
        ),
        visibility=TerminalVisibility.HIDDEN,
        backend_factory=_SilentBackend,
    )
    terminal_session = await manager.get_or_create("expired")
    await terminal_session.ensure_started()
    registry = ProcessRegistry(config)
    running = registry.create(
        command="sleep 999",
        terminal="expired",
        cwd=None,
        pid=None,
    )
    running.deadline_at = time.monotonic() - 1

    with patch(
        "modex_agent.tools.terminal.process_tool.check_process_writable",
        new=AsyncMock(return_value=None),
    ):
        result = await ProcessTool(registry, manager, config).execute(data="still there?")

    finished = registry.get_finished(running.id)
    assert "<status>timed_out</status>" in result
    assert "<message>" in result
    assert manager.get("expired") is None
    assert finished is running
    assert finished.status is ProcessStatus.TIMED_OUT
    assert finished.timed_out is True


# ─── Converged timeout finalization: close-first, mark-on-success ────────────


async def test_process_write_timeout_close_failure_returns_xml_and_leaves_running() -> None:
    """P2: a close exception must not escape the tool call — the timed_out
    XML is still returned, and the session stays RUNNING with its expired
    deadline so the watchdog retries close+mark on its next tick."""
    registry = ProcessRegistry()
    running = registry.create(command="sleep 999", terminal="expired", cwd=None, pid=None)
    running.deadline_at = time.monotonic() - 1
    terminal_session = MagicMock(spec=TerminalSession)
    terminal_session.name = "expired"
    terminal_session.write = AsyncMock()
    terminal_session.apply_outcome = MagicMock()
    manager = MagicMock(spec=TerminalManagerBase)
    manager.get_default = AsyncMock(return_value=terminal_session)
    manager.close = AsyncMock(side_effect=RuntimeError("close failed"))
    result = PollResult(outcome=PollOutcome.TIMED_OUT, output_parts=["partial"], elapsed_ms=1)

    with (
        patch(
            "modex_agent.tools.terminal.process_tool.check_process_writable",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "modex_agent.tools.terminal.process_tool._drain_terminal_after_action",
            new=AsyncMock(return_value=("partial", result)),
        ),
    ):
        xml = await ProcessTool(registry, manager).execute(data="still there?")

    assert "<status>timed_out</status>" in xml
    assert "<message>" in xml
    manager.close.assert_awaited_once_with("expired")
    terminal_session.apply_outcome.assert_called_once()
    assert registry.get_running(running.id) is running
    assert registry.get_finished(running.id) is None


async def test_command_timeout_marks_after_successful_close() -> None:
    """Converged contract: the TIMED_OUT mark happens only AFTER
    ``manager.close`` succeeds (including a False return = tab already
    gone), matching the watchdog's order."""
    config = TerminalRuntimeConfig(command_deadline_seconds=2)
    registry = ProcessRegistry(config)
    terminal_session = MagicMock(spec=TerminalSession)
    terminal_session.name = "default"
    terminal_session.backend_started = True
    terminal_session.ensure_started = AsyncMock()
    terminal_session.submit_command = AsyncMock()
    terminal_session.apply_outcome = MagicMock()
    manager = MagicMock(spec=TerminalManagerBase)
    manager.get_default = AsyncMock(return_value=terminal_session)
    manager.close = AsyncMock(return_value=True)
    result = PollResult(outcome=PollOutcome.TIMED_OUT, output_parts=["partial"], elapsed_ms=1)

    with (
        patch(
            "modex_agent.tools.terminal.command_tool.check_command_writable",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "modex_agent.tools.terminal.poll_loop.poll_until_settled",
            new=AsyncMock(return_value=result),
        ),
    ):
        xml = await CommandTool(manager, registry, config).execute(command="sleep 999")

    assert "<status>timed_out</status>" in xml
    manager.close.assert_awaited_once_with("default")
    finished = registry.get_finished_by_terminal("default")
    assert finished is not None
    assert finished.status is ProcessStatus.TIMED_OUT
    assert finished.timed_out is True
    terminal_session.apply_outcome.assert_called_once()


async def test_command_timeout_close_failure_returns_xml_and_leaves_running() -> None:
    """Converged contract, failure half: the close exception is logged and
    swallowed — the timed_out XML is still returned and the process stays
    RUNNING for the watchdog to retry close+mark."""
    config = TerminalRuntimeConfig(command_deadline_seconds=2)
    registry = ProcessRegistry(config)
    terminal_session = MagicMock(spec=TerminalSession)
    terminal_session.name = "default"
    terminal_session.backend_started = True
    terminal_session.ensure_started = AsyncMock()
    terminal_session.submit_command = AsyncMock()
    terminal_session.apply_outcome = MagicMock()
    manager = MagicMock(spec=TerminalManagerBase)
    manager.get_default = AsyncMock(return_value=terminal_session)
    manager.close = AsyncMock(side_effect=RuntimeError("close failed"))
    result = PollResult(outcome=PollOutcome.TIMED_OUT, output_parts=["partial"], elapsed_ms=1)

    with (
        patch(
            "modex_agent.tools.terminal.command_tool.check_command_writable",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "modex_agent.tools.terminal.poll_loop.poll_until_settled",
            new=AsyncMock(return_value=result),
        ),
    ):
        xml = await CommandTool(manager, registry, config).execute(command="sleep 999")

    assert "<status>timed_out</status>" in xml
    manager.close.assert_awaited_once_with("default")
    running = registry.get_running_by_terminal("default")
    assert running is not None
    assert registry.get_finished_by_terminal("default") is None
    terminal_session.apply_outcome.assert_called_once()
