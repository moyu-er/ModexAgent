"""Tests for TerminalSession."""

import asyncio

import pytest

from framework.tools.terminal.prompt import is_prompt_ready, sanitize_terminal_output
from framework.tools.terminal.session import TerminalSession
from framework.tools.terminal.types import Platform, ShellFamily, ShellInfo


@pytest.fixture(autouse=True)
def fast_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Speed up all asyncio.sleep calls in session tests so timeouts
    resolve quickly without wall-clock waiting."""
    _orig = asyncio.sleep

    async def _fast(delay: float) -> None:
        await _orig(min(delay, 0.001))

    monkeypatch.setattr(asyncio, "sleep", _fast)


class MockBackend:
    """Mock TerminalBackend for testing."""

    def __init__(self, visible: bool = False) -> None:
        self.alive = True
        self.buffer = ""
        self._started = False
        self.visible = visible
        self._backend_started = True

    async def start(self, shell: str | None = None, cwd: str | None = None, env: dict[str, str] | None = None) -> None:
        self._started = True
        self.alive = True

    async def write(self, data: str) -> None:
        self.buffer += data

    async def read(self, timeout: float = 5.0, max_size: int = 65536) -> str:
        # Short timeouts (trailing reads in drain) return empty so that
        # command output intended for the execute phase is not swallowed.
        if timeout <= 0.15:
            return ""
        return "mock-output\n$ "

    async def is_alive(self) -> bool:
        return self.alive

    async def terminate(self) -> None:
        self.alive = False

    async def kill(self) -> None:
        self.alive = False

    async def drain_startup(self) -> None:
        pass

    async def clear_input_line(self) -> None:
        pass


class StartupPromptBackend(MockBackend):
    """Backend that emits a shell prompt before command output is ready."""

    def __init__(self) -> None:
        super().__init__()
        self.reads = ["terminal escape setup", "", "startup banner\n$ ", "echo ready\nready\n$ "]

    async def drain_startup(self) -> None:
        """Consume startup reads during drain phase."""
        while self.reads:
            chunk = self.reads.pop(0)
            if is_prompt_ready(chunk):
                break

    async def read(self, timeout: float = 5.0, max_size: int = 65536) -> str:
        if timeout <= 0.15:
            return ""
        if self.reads:
            return self.reads.pop(0)
        return ""


class QueuedBackend(MockBackend):
    """Backend that returns queued chunks even on short drain reads."""

    def __init__(self, reads: list[str], visible: bool = False) -> None:
        super().__init__(visible=visible)
        self.reads = reads

    async def read(self, timeout: float = 5.0, max_size: int = 65536) -> str:
        if self.reads:
            return self.reads.pop(0)
        return ""


class CommandAwareBackend(MockBackend):
    """Backend with stale output before write and command output after write."""

    def __init__(self, stale_reads: list[str], command_reads: list[str]) -> None:
        super().__init__(visible=True)
        self.stale_reads = stale_reads
        self.command_reads = command_reads
        self.command_written = False

    async def write(self, data: str) -> None:
        await super().write(data)
        if "echo current" in data:
            self.command_written = True

    async def read(self, timeout: float = 5.0, max_size: int = 65536) -> str:
        if not self.command_written and self.stale_reads:
            return self.stale_reads.pop(0)
        if self.command_written and self.command_reads:
            return self.command_reads.pop(0)
        return ""


class TestTerminalSession:
    def test_session_creation(self) -> None:
        backend = MockBackend()
        session = TerminalSession(
            name="test",
            backend=backend,
            shell_info=ShellInfo(family=ShellFamily.BASH, path="/bin/bash", platform=Platform.LINUX),
        )
        assert session.name == "test"
        assert session.shell_info.name == "bash"

    @pytest.mark.asyncio
    async def test_execute_restarts_dead_backend(self) -> None:
        backend = MockBackend()
        backend.alive = False
        session = TerminalSession(
            name="test",
            backend=backend,
            shell_info=ShellInfo(family=ShellFamily.BASH, path="/bin/bash", platform=Platform.LINUX),
        )
        result = await session.execute("echo hello")
        assert backend._started is True
        assert "mock-output" in result

    @pytest.mark.asyncio
    async def test_execute_drains_startup_prompt_before_command(self) -> None:
        backend = StartupPromptBackend()
        backend.alive = False
        session = TerminalSession(
            name="test",
            backend=backend,
            shell_info=ShellInfo(family=ShellFamily.BASH, path="/bin/bash", platform=Platform.LINUX),
        )

        result = await session.execute("echo ready")

        assert "ready" in result
        assert "startup banner" not in result

    @pytest.mark.asyncio
    async def test_execute_reuses_live_session_with_line_clear_not_ctrl_c(self) -> None:
        """A shared terminal should clear dirty prompt input without interrupting jobs."""
        backend = MockBackend()
        session = TerminalSession(
            name="test",
            backend=backend,
            shell_info=ShellInfo(family=ShellFamily.BASH, path="/bin/bash", platform=Platform.WINDOWS),
        )

        await session.execute("echo first")
        backend.buffer = ""

        await session.execute("echo second")

        assert "\x03" not in backend.buffer
        assert backend.buffer == "echo second\n"

    @pytest.mark.asyncio
    async def test_execute_discards_pending_prompt_repaint_before_current_command(self) -> None:
        """Old terminal output must not satisfy the next tool call."""
        backend = CommandAwareBackend(
            stale_reads=["$ ", ""],
            command_reads=["echo current\ncurrent\n$ "],
        )
        session = TerminalSession(
            name="test",
            backend=backend,
            shell_info=ShellInfo(family=ShellFamily.BASH, path="/bin/bash", platform=Platform.WINDOWS),
        )
        session._needs_restart = False

        result = await session.execute("echo current")

        assert "current" in result
        assert result != "$ "
        assert "\x03" not in backend.buffer

    @pytest.mark.asyncio
    async def test_execute_records_history(self) -> None:
        backend = MockBackend()
        session = TerminalSession(
            name="test",
            backend=backend,
            shell_info=ShellInfo(family=ShellFamily.BASH, path="/bin/bash", platform=Platform.LINUX),
            max_history=2,
        )
        await session.execute("cmd1")
        await session.execute("cmd2")
        history = session.get_history()
        assert len(history) == 2
        assert history[0].command == "cmd1"
        assert history[1].command == "cmd2"

    @pytest.mark.asyncio
    async def test_history_truncation(self) -> None:
        backend = MockBackend()
        session = TerminalSession(
            name="test",
            backend=backend,
            shell_info=ShellInfo(family=ShellFamily.BASH, path="/bin/bash", platform=Platform.LINUX),
            max_history=2,
            history_truncate=10,
        )
        await session.execute("very_long_command_name")
        history = session.get_history()
        assert len(history[0].command) == 10

    @pytest.mark.asyncio
    async def test_terminal_info(self) -> None:
        backend = MockBackend()
        session = TerminalSession(
            name="test",
            backend=backend,
            shell_info=ShellInfo(family=ShellFamily.BASH, path="/bin/bash", platform=Platform.LINUX),
        )
        info = await session.to_info()
        assert info.name == "test"
        assert info.shell_type == "bash"
        assert info.command_count == 0

    @pytest.mark.asyncio
    async def test_history_ring_buffer_overflow(self) -> None:
        backend = MockBackend()
        session = TerminalSession(
            name="test",
            backend=backend,
            shell_info=ShellInfo(family=ShellFamily.BASH, path="/bin/bash", platform=Platform.LINUX),
            max_history=2,
        )
        await session.execute("cmd1")
        await session.execute("cmd2")
        await session.execute("cmd3")
        history = session.get_history()
        assert len(history) == 2
        assert history[0].command == "cmd2"
        assert history[1].command == "cmd3"

    @pytest.mark.asyncio
    async def test_get_state_and_restore(self) -> None:
        backend = MockBackend()
        session = TerminalSession(
            name="test",
            backend=backend,
            shell_info=ShellInfo(family=ShellFamily.BASH, path="/bin/bash", platform=Platform.LINUX),
            max_history=2,
        )
        await session.execute("echo hello")
        state = session.get_state()
        assert state["name"] == "test"
        assert state["shell_type"] == "bash"
        assert len(state["history"]) == 1
        assert state["created_at"] > 0

        backend2 = MockBackend()
        session2 = TerminalSession(
            name="test",
            backend=backend2,
            shell_info=ShellInfo(family=ShellFamily.BASH, path="/bin/bash", platform=Platform.LINUX),
            max_history=2,
        )
        session2.restore_state(state)
        assert session2.last_active == state["last_active"]
        assert session2.created_at == state["created_at"]
        assert len(session2.get_history()) == 1

    @pytest.mark.asyncio
    async def test_terminal_info_dead_backend(self) -> None:
        backend = MockBackend()
        session = TerminalSession(
            name="test",
            backend=backend,
            shell_info=ShellInfo(family=ShellFamily.BASH, path="/bin/bash", platform=Platform.LINUX),
        )
        info = await session.to_info()
        assert info.is_alive is False

    @pytest.mark.asyncio
    async def test_terminal_info_alive_after_execute(self) -> None:
        backend = MockBackend()
        session = TerminalSession(
            name="test",
            backend=backend,
            shell_info=ShellInfo(family=ShellFamily.BASH, path="/bin/bash", platform=Platform.LINUX),
        )
        await session.execute("echo hello")
        info = await session.to_info()
        assert info.is_alive is True


    @pytest.mark.asyncio
    async def test_execute_handles_shell_death(self) -> None:
        """When shell dies during command (e.g. 'exit'), execute must not hang."""

        class DyingBackend(MockBackend):
            def __init__(self) -> None:
                super().__init__()
                self._reads_after_death = 0

            async def write(self, data: str) -> None:
                if "exit" in data.lower():
                    self.alive = False

            async def read(self, timeout: float = 5.0, max_size: int = 65536) -> str:
                if not self.alive:
                    self._reads_after_death += 1
                    return ""
                return "mock-output\n$ "

        backend = DyingBackend()
        session = TerminalSession(
            name="test",
            backend=backend,
            shell_info=ShellInfo(family=ShellFamily.BASH, path="/bin/bash", platform=Platform.LINUX),
        )
        result = await session.execute("exit", timeout=5.0)
        assert "<status>ended</status>" in result
        assert backend._reads_after_death < 10  # should break early, not spin

    @pytest.mark.asyncio
    async def test_execute_returns_partial_output_on_timeout(self) -> None:
        """When timeout expires, return structured result with output and timeout warning."""

        class PartialOutputBackend(MockBackend):
            def __init__(self) -> None:
                super().__init__()
                self._started = False
                self._reads = ["$ ", "partial output\n"]
                self._idx = 0

            async def start(self, shell: str | None = None, cwd: str | None = None, env: dict[str, str] | None = None) -> None:
                await super().start(shell, cwd, env)
                self._started = True

            async def drain_startup(self) -> None:
                if self._idx < len(self._reads) and is_prompt_ready(self._reads[self._idx]):
                    self._idx += 1

            async def read(self, timeout: float = 5.0, max_size: int = 65536) -> str:
                if timeout <= 0.15:
                    return ""
                if not self._started:
                    return ""
                if self._idx < len(self._reads):
                    out = self._reads[self._idx]
                    self._idx += 1
                    return out
                return ""

        backend = PartialOutputBackend()
        session = TerminalSession(
            name="test",
            backend=backend,
            shell_info=ShellInfo(family=ShellFamily.BASH, path="/bin/bash", platform=Platform.LINUX),
        )
        result = await session.execute("long_cmd", timeout=0.05)
        assert "partial output" in result
        assert "<shell_result>" in result
        assert "<output>" in result
        assert "<status>timeout</status>" in result

    @pytest.mark.asyncio
    async def test_execute_after_timeout_reports_busy_without_writing_command(self) -> None:
        """A timed-out process stays foreground; later commands must not overwrite it."""

        class SlowBackend(MockBackend):
            async def read(self, timeout: float = 5.0, max_size: int = 65536) -> str:
                if timeout <= 0.15:
                    return ""
                return "partial output\n"

        backend = SlowBackend()
        session = TerminalSession(
            name="test",
            backend=backend,
            shell_info=ShellInfo(family=ShellFamily.BASH, path="/bin/bash", platform=Platform.WINDOWS),
        )

        first = await session.execute("long_cmd", timeout=0.05)
        backend.buffer = ""
        second = await session.execute("echo after-timeout", timeout=1.0)

        assert "<status>timeout</status>" in first
        assert "<status>busy</status>" in second
        assert "echo after-timeout" not in backend.buffer
        assert "\x03" not in backend.buffer

    @pytest.mark.asyncio
    async def test_execute_no_empty_read_shortcut(self) -> None:
        """Silent commands must NOT trigger the old 'waiting for input' shortcut."""

        class SilentBackend(MockBackend):
            async def read(self, timeout: float = 5.0, max_size: int = 65536) -> str:
                return ""  # Always empty

        backend = SilentBackend()
        session = TerminalSession(
            name="test",
            backend=backend,
            shell_info=ShellInfo(family=ShellFamily.BASH, path="/bin/bash", platform=Platform.LINUX),
        )
        result = await session.execute("sleep 10", timeout=0.5)
        assert "<status>timeout</status>" in result
        assert "<output>" in result

    @pytest.mark.asyncio
    async def test_backend_survives_after_normal_command(self) -> None:
        """After a normal command, the backend must stay alive and be reused."""

        class TrackingBackend(MockBackend):
            def __init__(self) -> None:
                super().__init__()
                self.start_calls = 0
                self.terminate_calls = 0

            async def start(
                self,
                shell: str | None = None,
                cwd: str | None = None,
                env: dict[str, str] | None = None,
            ) -> None:
                self.start_calls += 1
                self._started = True
                self.alive = True

            async def terminate(self) -> None:
                self.terminate_calls += 1
                self.alive = False

        backend = TrackingBackend()
        session = TerminalSession(
            name="test",
            backend=backend,
            shell_info=ShellInfo(family=ShellFamily.BASH, path="/bin/bash", platform=Platform.LINUX),
        )

        await session.execute("echo first")
        assert backend.start_calls == 1  # started on first execute
        assert backend.terminate_calls == 0
        assert backend.alive is True

        await session.execute("echo second")
        assert backend.start_calls == 1  # NOT restarted — reused
        assert backend.terminate_calls == 0
        assert backend.alive is True

    @pytest.mark.asyncio
    async def test_only_exit_terminates_backend(self) -> None:
        """Only exit/logout/quit should terminate the backend; normal commands survive."""

        class TrackingBackend(MockBackend):
            def __init__(self) -> None:
                super().__init__()
                self.start_calls = 0
                self.terminate_calls = 0

            async def start(
                self,
                shell: str | None = None,
                cwd: str | None = None,
                env: dict[str, str] | None = None,
            ) -> None:
                self.start_calls += 1
                self._started = True
                self.alive = True

            async def terminate(self) -> None:
                self.terminate_calls += 1
                self.alive = False

            async def read(self, timeout: float = 5.0, max_size: int = 65536) -> str:
                return "$ "

        backend = TrackingBackend()
        session = TerminalSession(
            name="test",
            backend=backend,
            shell_info=ShellInfo(family=ShellFamily.BASH, path="/bin/bash", platform=Platform.LINUX),
        )

        await session.execute("echo hello")
        assert backend.alive is True
        assert backend.terminate_calls == 0

        await session.execute("exit")
        assert backend.terminate_calls == 1
        assert backend.alive is False

        # After exit, _needs_restart is True so next execute restarts
        await session.execute("echo after")
        assert backend.start_calls == 2  # restarted after exit
        assert backend.alive is True

    @pytest.mark.asyncio
    async def test_is_waiting_for_input_detects_password_prompt(self) -> None:
        """_is_waiting_for_input should match common input prompts."""
        backend = MockBackend()
        session = TerminalSession(
            name="test",
            backend=backend,
            shell_info=ShellInfo(family=ShellFamily.BASH, path="/bin/bash", platform=Platform.LINUX),
        )
        assert session._is_waiting_for_input("sudo apt install foo\n[sudo] password for user: ")
        assert session._is_waiting_for_input("ssh user@host\nuser@host's password: ")
        assert session._is_waiting_for_input("Enter password: ")
        assert session._is_waiting_for_input("login: ")
        assert session._is_waiting_for_input("Username: ")
        assert session._is_waiting_for_input("Do you want to continue? [Y/n] ")
        assert not session._is_waiting_for_input("echo hello\nhello\n$ ")
        assert not session._is_waiting_for_input("")

    @pytest.mark.asyncio
    async def test_is_waiting_for_input_detects_extended_prompts(self) -> None:
        """_is_waiting_for_input should match common prompts beyond password."""
        backend = MockBackend()
        session = TerminalSession(
            name="test",
            backend=backend,
            shell_info=ShellInfo(family=ShellFamily.BASH, path="/bin/bash", platform=Platform.LINUX),
        )
        # PIN / Token / Passcode
        assert session._is_waiting_for_input("Enter PIN: ")
        assert session._is_waiting_for_input("Token: ")
        assert session._is_waiting_for_input("passcode: ")
        # Verification / 2FA
        assert session._is_waiting_for_input("Verification code: ")
        assert session._is_waiting_for_input("2FA code: ")
        assert session._is_waiting_for_input("OTP: ")
        # Key press
        assert session._is_waiting_for_input("Press any key to continue...")
        # File overwrite
        assert session._is_waiting_for_input("overwrite existing file? (y/n) ")
        assert session._is_waiting_for_input("Replace file? [Y/n] ")
        # Confirmation
        assert session._is_waiting_for_input("Confirm deletion? ")
        assert session._is_waiting_for_input("Do you want to proceed? (yes/no) ")
        # Password variants
        assert session._is_waiting_for_input("Current password: ")
        assert session._is_waiting_for_input("New password: ")
        assert session._is_waiting_for_input("Retype password: ")
        assert session._is_waiting_for_input("Repeat password: ")
        # SSH yes/no
        assert session._is_waiting_for_input("Are you sure you want to continue connecting (yes/no)? ")
        # Case variations
        assert session._is_waiting_for_input("Enter PassCode: ")
        assert session._is_waiting_for_input("[y/N] ")
        assert session._is_waiting_for_input("(Y/n) ")

    @pytest.mark.asyncio
    async def test_execute_prompt_detection_with_ansi_colored_output(self) -> None:
        """When command output contains ANSI escape sequences, prompt detection
        still works via is_prompt_ready()."""

        class AnsiOutputBackend(MockBackend):
            def __init__(self) -> None:
                super().__init__()
                self._reads = [
                    "hello\n\x1b[32mworld\x1b[0m\nPS C:\\test> ",
                ]
                self._idx = 0

            async def drain_startup(self) -> None:
                pass

            async def read(self, timeout: float = 5.0, max_size: int = 65536) -> str:
                if timeout <= 0.15:
                    return ""
                if self._idx < len(self._reads):
                    out = self._reads[self._idx]
                    self._idx += 1
                    return out
                return ""

        backend = AnsiOutputBackend()
        session = TerminalSession(
            name="test",
            backend=backend,
            shell_info=ShellInfo(
                family=ShellFamily.BASH, path="/bin/bash", platform=Platform.LINUX
            ),
        )
        result = await session.execute("echo test", timeout=2.0)

        assert "<status>timeout</status>" not in result, (
            "Prompt detection failed with ANSI colored output — timed out"
        )
        assert "world" in result

    @pytest.mark.asyncio
    async def test_execute_returns_early_on_input_prompt(self) -> None:
        """When the command emits an input prompt, execute should return immediately
        instead of waiting for the full timeout.
        """

        class PasswordPromptBackend(MockBackend):
            _call_count = 0

            async def drain_startup(self) -> None:
                self._call_count += 1

            async def read(self, timeout: float = 5.0, max_size: int = 65536) -> str:
                if timeout <= 0.15:
                    return ""
                self._call_count += 1
                if self._call_count == 1:
                    return "$ "
                if self._call_count == 2:
                    return "[sudo] password for user: "
                # After the prompt, simulate silence (no more output)
                return ""

        backend = PasswordPromptBackend()
        session = TerminalSession(
            name="test",
            backend=backend,
            shell_info=ShellInfo(family=ShellFamily.BASH, path="/bin/bash", platform=Platform.LINUX),
        )
        result = await session.execute("sudo apt update", timeout=5.0)
        assert "password" in result.lower()
        assert "<status>waiting_input</status>" in result
        assert "<output>" in result
        # Should NOT have timed out — returned early because of prompt detection
        assert "<status>timeout</status>" not in result
        assert backend.alive is True  # backend stays alive

    @pytest.mark.asyncio
    async def test_execute_returns_sanitized_output(self) -> None:
        """Model-facing output must not contain raw ANSI escape sequences."""

        class ColoredOutputBackend(MockBackend):
            def __init__(self) -> None:
                super().__init__()
                self._reads = [
                    "\x1b[0;33m95691ee\x1b[0m fix(terminal): stabilize\x1b[0K\r\n"
                    "\x1b[0;32;92mgyt@XXSDDM\x1b[0m$\x1b[0K\x1b[71G\x1b[?25h",
                ]
                self._idx = 0

            async def read(self, timeout: float = 5.0, max_size: int = 65536) -> str:
                if timeout <= 0.15:
                    return ""
                if self._idx < len(self._reads):
                    out = self._reads[self._idx]
                    self._idx += 1
                    return out
                return ""

        backend = ColoredOutputBackend()
        session = TerminalSession(
            name="test",
            backend=backend,
            shell_info=ShellInfo(family=ShellFamily.BASH, path="/bin/bash", platform=Platform.LINUX),
        )
        result = await session.execute("git log --oneline -1", timeout=2.0)
        assert "\x1b[" not in result
        assert "95691ee" in result
        assert "stabilize" in result
        assert "gyt@XXSDDM$" in result

    @pytest.mark.asyncio
    async def test_waiting_input_skips_clear_input_line(self) -> None:
        """After returning waiting_input (e.g. sudo password prompt), the next
        execute() must NOT call clear_input_line() so that the password input
        is not destroyed."""

        class PasswordPromptBackend(MockBackend):
            _call_count = 0
            clear_calls = 0

            async def drain_startup(self) -> None:
                self._call_count += 1

            async def clear_input_line(self) -> None:
                self.clear_calls += 1

            async def read(self, timeout: float = 5.0, max_size: int = 65536) -> str:
                if timeout <= 0.15:
                    return ""
                self._call_count += 1
                if self._call_count == 1:
                    return "$ "
                if self._call_count == 2:
                    return "[sudo] password for user: "
                return ""

        backend = PasswordPromptBackend()
        session = TerminalSession(
            name="pwd-test",
            backend=backend,
            shell_info=ShellInfo(
                family=ShellFamily.BASH, path="/bin/bash", platform=Platform.LINUX
            ),
        )

        # First execute: sudo command triggers waiting_input
        result = await session.execute("sudo apt update", timeout=2.0)
        assert "<status>waiting_input</status>" in result
        assert "password" in result.lower()

        # Second execute: send password — clear_input_line must NOT be called
        backend.clear_calls = 0
        await session.execute("mypassword", timeout=2.0)
        assert backend.clear_calls == 0, (
            f"clear_input_line called {backend.clear_calls} times after waiting_input"
        )
        assert "mypassword" in backend.buffer

    @pytest.mark.asyncio
    async def test_send_interrupt_writes_ctrl_c(self) -> None:
        """send_interrupt() must write \x03 (Ctrl+C) to the backend."""
        backend = MockBackend()
        session = TerminalSession(
            name="test",
            backend=backend,
            shell_info=ShellInfo(
                family=ShellFamily.BASH, path="/bin/bash", platform=Platform.LINUX
            ),
        )
        await session.send_interrupt()
        assert "\x03" in backend.buffer

    @pytest.mark.asyncio
    async def test_send_interrupt_clears_busy_after_timeout(self) -> None:
        """After timeout, send_interrupt() must clear busy so next execute works."""

        class SlowBackend(MockBackend):
            async def read(self, timeout: float = 5.0, max_size: int = 65536) -> str:
                if timeout <= 0.15:
                    return ""
                return "partial output\n"

        backend = SlowBackend()
        session = TerminalSession(
            name="test",
            backend=backend,
            shell_info=ShellInfo(
                family=ShellFamily.BASH, path="/bin/bash", platform=Platform.WINDOWS
            ),
        )

        first = await session.execute("long_cmd", timeout=0.05)
        assert "<status>timeout</status>" in first
        assert session._busy_after_timeout is True

        await session.send_interrupt()
        assert session._busy_after_timeout is False
        assert "\x03" in backend.buffer
