"""Tests for TerminalSessionExecutor."""

import tempfile
from collections.abc import Generator
from pathlib import Path

import pytest

from framework.tools.standard.shell_tool import TerminalSessionExecutor
from framework.tools.terminal.manager import TerminalManager


class MockBackend:
    def __init__(self) -> None:
        self.alive = True
        self.buffer = ""
        self._started = False
        self._startup_drained = False
        self._backend_started = True

    async def start(
        self,
        shell: str | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self._started = True

    async def write(self, data: str) -> None:
        self.buffer += data

    async def read(self, timeout: float = 5.0, max_size: int = 65536) -> str:
        if not self._startup_drained:
            self._startup_drained = True
            return "$ "
        return "mock-output\n$ "

    async def is_alive(self) -> bool:
        return self.alive

    async def drain_startup(self) -> None:
        self._startup_drained = True

    async def terminate(self) -> None:
        self.alive = False

    async def kill(self) -> None:
        self.alive = False

    async def clear_input_line(self) -> None:
        pass


class TestTerminalSessionExecutor:
    @pytest.fixture
    def executor(self) -> Generator[TerminalSessionExecutor]:
        with tempfile.TemporaryDirectory() as td:
            tm = TerminalManager(
                storage_dir=Path(td),
                max_terminals=3,
                backend_factory=MockBackend,
            )
            yield TerminalSessionExecutor(terminal_manager=tm)

    @pytest.mark.asyncio
    async def test_execute_creates_default_terminal(self, executor: TerminalSessionExecutor) -> None:
        """If no default terminal exists, execute should auto-create one."""
        result = await executor.execute("echo hello")
        assert isinstance(result, str)

    @pytest.mark.asyncio
    async def test_execute_reuses_existing_session(self, executor: TerminalSessionExecutor) -> None:
        """Second execute() must reuse the same TerminalSession, not create a new one."""
        await executor.execute("echo first")
        session1 = await executor._tm.get_default_session()
        assert session1 is not None

        await executor.execute("echo second")
        session2 = await executor._tm.get_default_session()
        assert session2 is session1  # same object -- reused, not recreated

    def test_shell_info_reflects_manager_default(self, executor: TerminalSessionExecutor) -> None:
        info = executor.shell_info()
        assert info.name in ("bash", "zsh", "sh", "cmd")

    @pytest.mark.asyncio
    async def test_execute_quotes_working_dir_with_spaces(self, executor: TerminalSessionExecutor) -> None:
        """When working_dir contains spaces, cd command must wrap the path in quotes."""
        # First call creates the default session
        await executor.execute("echo first")
        # Second call reuses session and should send quoted cd
        await executor.execute("echo hello", working_dir="C:\\Program Files")
        session = await executor._tm.get_default_session()
        assert session is not None
        # The cd command should quote the path to avoid splitting
        assert 'cd "C:\\Program Files"' in session._backend.buffer

    @pytest.mark.asyncio
    async def test_shell_uses_existing_session_when_no_default_set(self, executor: TerminalSessionExecutor) -> None:
        """When multiple sessions exist but no default is set, ShellTool should use
        the most recently active one instead of creating a new 'default' session.
        """
        from framework.tools.terminal.tool import TerminalTool
        tool = TerminalTool(executor._tm)

        # TerminalTool opens two sessions (no "default" yet)
        await tool.execute(action="open", name="tab-1")
        await tool.execute(action="open", name="tab-2")

        # Simulate no explicit default (user never selected one)
        executor._tm._default_terminal = None

        # get_default_session() no longer falls back to other tabs.
        fallback = await executor._tm.get_default_session()
        assert fallback is None

        # ShellTool execute should create a new "default" since no default is set.
        await executor.execute("echo hello")
        assert "default" in executor._tm.list_names()
        default = await executor._tm.get_default_session()
        assert default is not None
        assert default.name == "default"

    @pytest.mark.asyncio
    async def test_shell_recreates_terminal_after_closing_only_session(self, executor: TerminalSessionExecutor) -> None:
        """After closing the only session, ShellTool should recreate a new terminal session."""
        from framework.tools.terminal.tool import TerminalTool
        tool = TerminalTool(executor._tm)

        # Create one session via ShellTool
        await executor.execute("echo first")
        session1 = await executor._tm.get_default_session()
        assert session1 is not None
        assert session1.name == "default"

        # Close the only session via TerminalTool
        result = await tool.execute(action="close", name="default")
        assert "Closed" in result

        # Verify session is gone
        assert await executor._tm.get_default_session() is None

        # ShellTool should create a new terminal session (not fallback to Subprocess)
        await executor.execute("echo second")
        session2 = await executor._tm.get_default_session()
        assert session2 is not None
        assert session2.name == "default"
        # The executor should report stateful (TerminalSession, not Subprocess)
        info = executor.shell_info()
        assert info.name in ("bash", "zsh", "sh", "cmd")

    @pytest.mark.asyncio
    async def test_shell_execute_ctrl_c_interrupts_busy_session(self, executor: TerminalSessionExecutor) -> None:
        """After a command times out, shell.execute('^C') must send Ctrl+C
        so the agent can interrupt without switching to the terminal tool."""

        class SlowThenPromptBackend(MockBackend):
            def __init__(self) -> None:
                super().__init__()
                self.interrupted = False

            async def read(self, timeout: float = 5.0, max_size: int = 65536) -> str:
                if timeout <= 0.15:
                    return ""
                if self.interrupted:
                    return "$ "
                return "partial output\n"

        # Replace the backend with a slow one
        session = await executor._tm.get_or_create("default")
        backend = SlowThenPromptBackend()
        session._backend = backend
        session._needs_restart = False
        session._backend_started = True

        # 1. Run a slow command → timeout
        first = await executor.execute("sleep 100", timeout=0.05)
        assert "<status>timeout</status>" in first
        assert session._busy_after_timeout is True

        # 2. Normal command is blocked with busy
        second = await executor.execute("echo done", timeout=1.0)
        assert "<status>busy</status>" in second

        # 3. Send ^C via shell tool → interrupt
        backend.interrupted = True
        third = await executor.execute("^C", timeout=1.0)
        assert "Ctrl+C" in third or "interrupt" in third.lower()
        assert session._busy_after_timeout is False
        assert "\x03" in backend.buffer

        # 4. Normal command works again
        fourth = await executor.execute("echo done", timeout=1.0)
        assert "<status>busy</status>" not in fourth
        assert "<status>timeout</status>" not in fourth
