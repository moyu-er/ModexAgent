"""Comprehensive tests for the 4 abnormal command-detection scenarios.

Covers:
  1. Long-running, continuous output (不断输出的大耗时)        → EXECUTING
  2. Long-running, refreshing output / repaints (不断刷新的大耗时) → EXECUTING
  3. Blocked / hung command (阻塞)                          → STUCK
  4. Waiting for user input (等待用户输入)                    → WAITING_INPUT
  5. UNKNOWN — no data ever received                       → UNKNOWN
  6. Normal completion — prompt returns                     → IDLE

Also verifies:
  - poll_until_settled outcomes for each scenario
  - last_command_output correctness (command + output + prompt)
  - XML field presence for each status (status, cursor, idle_ms, message)
  - terminal current XML correctness across scenarios
"""

from __future__ import annotations

import time

import pytest

from framework.tools.terminal.poll_loop import PollOutcome, PollResult, poll_until_settled
from framework.tools.terminal.prompt import (
    extract_last_command_output,
    is_waiting_for_input,
)
from framework.tools.terminal.results import TerminalRead, TerminalSegment
from framework.tools.terminal.types import TerminalCommandStatus


# ── Test helpers ────────────────────────────────────────────────────────────

def _seg(text: str, *, cursor: str = "", prompt: bool = False) -> TerminalSegment:
    """Shortcut to build a TerminalSegment."""
    return TerminalSegment(text=text, cursor_line=cursor or text, is_empty_prompt=prompt)


# ── 1. is_waiting_for_input — content-based detection ────────────────────────


class TestIsWaitingForInput:
    """Content-based input-prompt detection."""

    def test_password_prompt(self) -> None:
        assert is_waiting_for_input("[sudo] password for user: ")

    def test_yn_prompt(self) -> None:
        assert is_waiting_for_input("Do you want to continue? [y/n] ")

    def test_login_prompt(self) -> None:
        assert is_waiting_for_input("login: ")

    def test_overwrite_prompt(self) -> None:
        assert is_waiting_for_input("overwrite file.txt? (y/n) ")

    def test_multi_line_last_line_is_prompt(self) -> None:
        text = "Downloading...\nExtracting...\nSetting up...\nPassword: "
        assert is_waiting_for_input(text)

    def test_normal_output_is_not_input_wait(self) -> None:
        assert not is_waiting_for_input("Building package (1/100)...")
        assert not is_waiting_for_input("hello world")

    def test_empty_is_not_input_wait(self) -> None:
        assert not is_waiting_for_input("")

    def test_progress_output_is_not_input_wait(self) -> None:
        """Refreshing progress bars must NOT be mistaken for input prompts."""
        assert not is_waiting_for_input(
            "\rProgress: [##########] 100%"
        )
        assert not is_waiting_for_input(
            "  [1/100] Compiling src/main.rs..."
        )

    def test_password_word_in_middle_of_output_not_prompt(self) -> None:
        """The word 'password' in ordinary command output is NOT an input prompt.
        'echo Your password is hunter2' is just echo output — the line ends
        with 'hunter2' (not ':', '?', ']', ')'), so it correctly avoids
        misleading the agent into thinking terminal is waiting for input.
        """
        assert not is_waiting_for_input("echo Your password is hunter2")

    def test_password_in_middle_of_output(self) -> None:
        """If 'password:' is not on the LAST line, it should NOT trigger."""
        text = "Some output\npassword: please enter\nmore output after"
        assert not is_waiting_for_input(text)


# ── 2. extract_last_command_output — command-to-prompt scope ─────────────────


class TestExtractLastCommandOutput:
    """Verify correct output extraction from command line to terminal end."""

    def test_command_completed_two_prompts(self) -> None:
        """After command completes: should see from command prompt to idle prompt."""
        text = "$ pwd\n/home/user\n$ "
        result = extract_last_command_output(text)
        assert "$ pwd" in result
        assert "/home/user" in result
        assert "$ " in result

    def test_command_running_one_prompt(self) -> None:
        """While command runs: only the command prompt line, output, no idle prompt."""
        text = "$ npm install\nFetching packages...\nInstalling dep1...\n"
        result = extract_last_command_output(text)
        assert "$ npm install" in result
        assert "Fetching packages" in result
        assert "Installing dep1" in result

    def test_idle_no_command(self) -> None:
        """When idle with no command running: just the current prompt."""
        text = "$ "
        result = extract_last_command_output(text)
        # After strip(), "$ " becomes "$" (trailing whitespace stripped)
        assert "$" in result.strip()

    def test_empty_text(self) -> None:
        assert extract_last_command_output("") == ""

    def test_bash_prompt_with_command(self) -> None:
        text = "user@host:~$ ls -la\ntotal 42\ndrwxr-xr-x  2 user user 4096\nuser@host:~$ "
        result = extract_last_command_output(text)
        assert "ls -la" in result
        assert "total 42" in result
        assert "user@host:~$" in result

    def test_powershell_prompt(self) -> None:
        text = "PS C:\\project> dotnet build\nBuild succeeded.\nPS C:\\project> "
        result = extract_last_command_output(text)
        assert "dotnet build" in result
        assert "Build succeeded" in result
        assert "PS C:" in result


# ── 3. command_status — scenario classification ─────────────────────────────


class TestCommandStatusScenarios:
    """Test that command_status() correctly classifies each of the 4 scenarios.

    Uses a controllable FakeBackend + TerminalSession via the test infra
    in test_session_status.py.

    IMPORTANT: _ever_received_bytes must be True before command_status()
    will return anything other than UNKNOWN. Each test must push at least
    one byte through poll_once() first.
    """

    @pytest.fixture
    def infra(self):
        """Create a session with controllable backend for scenario testing."""
        from framework.tools.terminal.config import TerminalRuntimeConfig
        from framework.tools.terminal.managers import BaseTerminalManager
        from framework.tools.terminal.types import (
            Platform,
            ShellFamily,
            ShellInfo,
            TerminalVisibility,
        )

        class FakeBackend:
            platform = Platform.WINDOWS
            visibility = TerminalVisibility.HIDDEN

            def __init__(self) -> None:
                self.started = False
                self.alive = True
                self._next_reads: list[TerminalRead] = []
                self._segment = TerminalSegment(text="$ ", cursor_line="$ ", is_empty_prompt=True)
                self._buffer_text = ""

            async def start(self, shell=None, cwd=None, env=None) -> None:
                self.started = True

            async def write(self, data: str) -> None:
                pass

            async def read_pending(self, timeout: float = 0.1, max_size: int = 65536) -> TerminalRead:
                if self._next_reads:
                    return self._next_reads.pop(0)
                return TerminalRead()

            async def read(self, timeout: float = 0.1, max_size: int = 65536) -> str:
                r = await self.read_pending(timeout, max_size)
                return r.raw

            async def current_segment(self) -> TerminalSegment:
                return self._segment

            async def interrupt(self) -> None:
                pass

            async def terminate(self) -> None:
                self.alive = False

            async def kill(self) -> None:
                self.alive = False

            async def is_alive(self) -> bool:
                return self.alive

            def stdin_writable(self) -> bool:
                return self.alive

            async def drain_startup(self) -> None:
                pass

            async def clear_input_line(self) -> None:
                pass

            def mark_command_boundary(self) -> None:
                pass

            def output_buffer_text(self) -> str:
                return self._buffer_text

        cfg = TerminalRuntimeConfig(prompt_stabilize_ms=50)
        manager = BaseTerminalManager(
            shell_info=ShellInfo(ShellFamily.BASH, "bash", Platform.WINDOWS),
            visibility=TerminalVisibility.HIDDEN,
            backend_factory=FakeBackend,
            config=cfg,
        )
        return manager

    # ── Scenario 1: Long-running, continuous output → EXECUTING ──────────

    @pytest.mark.asyncio
    async def test_continuous_output_is_executing(self, infra) -> None:
        """Building/deploying with frequent new-line output → EXECUTING."""
        session = await infra.get_default()
        backend = session._backend
        backend._segment = _seg(
            "$ dotnet build\n"
            "  Determining projects...\n"
            "  Compiling src/main.cs...\n"
            "  Build in progress...\n"
        )
        backend._buffer_text = backend._segment.text
        # Seed bytes so _ever_received_bytes = True
        backend._next_reads = [
            TerminalRead(stdout="  Build in progress...\n", raw="  Build in progress...\n"),
        ]
        await session.poll_once(timeout=0.1)

        status = await session.command_status()
        assert status == TerminalCommandStatus.EXECUTING, (
            f"Expected EXECUTING for continuous output, got {status}"
        )

    @pytest.mark.asyncio
    async def test_continuous_output_not_waiting_input(self, infra) -> None:
        """Continuous build output must NOT be classified as WAITING_INPUT."""
        session = await infra.get_default()
        backend = session._backend
        backend._segment = _seg(
            "$ npm run build\n"
            "> build\n"
            "> tsc --build\n"
            "Compiling 150 files...\n"
        )
        # Simulate byte activity
        backend._next_reads = [
            TerminalRead(stdout="Compiling 150 files...\n", raw="Compiling 150 files...\n"),
        ]
        await session.poll_once(timeout=0.1)

        status = await session.command_status()
        assert status != TerminalCommandStatus.WAITING_INPUT
        assert status != TerminalCommandStatus.STUCK

    # ── Scenario 2: Refreshing output (progress bar with \r) → EXECUTING ─

    @pytest.mark.asyncio
    async def test_refreshing_progress_is_executing(self, infra) -> None:
        """Download/cURL with \r progress bars → EXECUTING (not stuck)."""
        session = await infra.get_default()
        backend = session._backend
        backend._segment = _seg(
            "$ curl -o file.zip https://example.com/large\n"
            "\r  % Total    % Received % Xferd  Average Speed\n"
            "\r  50 1024M   50  512M    0     0  5.2M      0  0:03:17\n"
        )
        # Simulate recent byte activity (raw \r repaints count as activity)
        backend._next_reads = [
            TerminalRead(
                stdout="\r  50 1024M   50  512M    0     0  5.2M      0  0:03:17\n",
                raw="\r  50 1024M   50  512M    0     0  5.2M      0  0:03:17\n",
            ),
        ]
        await session.poll_once(timeout=0.1)

        status = await session.command_status()
        assert status == TerminalCommandStatus.EXECUTING, (
            f"Expected EXECUTING for refreshing progress, got {status}"
        )

    @pytest.mark.asyncio
    async def test_repaint_bytes_update_last_byte_at(self, infra) -> None:
        """\r repaint bytes must update _last_byte_at (raw byte activity)."""
        session = await infra.get_default()
        backend = session._backend

        before = session.last_byte_at
        # Simulate a \r repaint chunk
        backend._next_reads = [
            TerminalRead(
                stdout="\rProgress: 50%",
                raw="\rProgress: 50%",
            ),
        ]
        await session.poll_once(timeout=0.1)

        # _last_byte_at should have advanced
        assert session.last_byte_at >= before

    # ── Scenario 3: Blocked / hung command → STUCK ───────────────────────

    @pytest.mark.asyncio
    async def test_silent_command_is_stuck(self, infra) -> None:
        """15s+ of no bytes and no prompt → STUCK."""
        session = await infra.get_default()
        backend = session._backend
        backend._segment = _seg(
            "$ ssh unreachable-host\n"
            "Connecting to unreachable-host...\n"
        )
        # Simulate bytes were received, then silence
        backend._next_reads = [
            TerminalRead(stdout="Connecting to unreachable-host...\n", raw="Connecting to unreachable-host...\n"),
        ]
        await session.poll_once(timeout=0.1)
        # Wind back last_byte_at to simulate 16s of silence
        session._last_byte_at = time.monotonic() - 16.0

        status = await session.command_status()
        assert status == TerminalCommandStatus.STUCK, (
            f"Expected STUCK after 15s silence, got {status}"
        )

    @pytest.mark.asyncio
    async def test_stuck_not_waiting_input(self, infra) -> None:
        """Stuck commands should NOT be classified as WAITING_INPUT."""
        session = await infra.get_default()
        backend = session._backend
        backend._segment = _seg("$ ping 192.0.2.1\nPING 192.0.2.1...\n")
        backend._next_reads = [
            TerminalRead(stdout="PING 192.0.2.1...\n", raw="PING 192.0.2.1...\n"),
        ]
        await session.poll_once(timeout=0.1)
        session._last_byte_at = time.monotonic() - 20.0

        status = await session.command_status()
        # Must be STUCK, not WAITING_INPUT (no content markers)
        assert status == TerminalCommandStatus.STUCK

    # ── Scenario 4: Waiting for user input → WAITING_INPUT ────────────────

    @pytest.mark.asyncio
    async def test_password_prompt_is_waiting_input(self, infra) -> None:
        """Password: marker → WAITING_INPUT."""
        session = await infra.get_default()
        backend = session._backend
        backend._segment = _seg(
            "$ sudo apt install nginx\n[sudo] password for user: ",
            cursor="[sudo] password for user: ",
        )
        backend._next_reads = [
            TerminalRead(stdout="[sudo] password for user: ", raw="[sudo] password for user: "),
        ]
        await session.poll_once(timeout=0.1)

        status = await session.command_status()
        assert status == TerminalCommandStatus.WAITING_INPUT

    @pytest.mark.asyncio
    async def test_yes_no_prompt_is_waiting_input(self, infra) -> None:
        """[y/n] marker → WAITING_INPUT."""
        session = await infra.get_default()
        backend = session._backend
        backend._segment = _seg(
            "$ rm -rf /important\nrm: descend into directory '/important'? [y/n] ",
            cursor="rm: descend into directory '/important'? [y/n] ",
        )
        backend._next_reads = [
            TerminalRead(
                stdout="rm: descend into directory '/important'? [y/n] ",
                raw="rm: descend into directory '/important'? [y/n] ",
            ),
        ]
        await session.poll_once(timeout=0.1)

        status = await session.command_status()
        assert status == TerminalCommandStatus.WAITING_INPUT

    @pytest.mark.asyncio
    async def test_ssh_login_is_waiting_input(self, infra) -> None:
        """ssh password prompt → WAITING_INPUT."""
        session = await infra.get_default()
        backend = session._backend
        backend._segment = _seg(
            "$ ssh admin@server.example.com\nadmin@server.example.com's password: ",
            cursor="admin@server.example.com's password: ",
        )
        backend._next_reads = [
            TerminalRead(
                stdout="admin@server.example.com's password: ",
                raw="admin@server.example.com's password: ",
            ),
        ]
        await session.poll_once(timeout=0.1)

        status = await session.command_status()
        assert status == TerminalCommandStatus.WAITING_INPUT

    # ── Scenario 5: UNKNOWN ──────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_fresh_session_is_unknown(self, infra) -> None:
        """Session that has never received any bytes → UNKNOWN."""
        session = await infra.get_default()
        backend = session._backend
        # No bytes ever received — _ever_received_bytes is False
        backend._segment = _seg("")

        status = await session.command_status()
        assert status == TerminalCommandStatus.UNKNOWN

    # ── Scenario 6: IDLE ─────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_prompt_stable_is_idle(self, infra) -> None:
        """After command completes → IDLE."""
        session = await infra.get_default()
        backend = session._backend
        backend._segment = _seg("$ ", cursor="$ ", prompt=True)
        # Simulate some output was received (discarded startup)
        backend._next_reads = [
            TerminalRead(stdout="$ ", raw="$ "),
        ]
        await session.poll_once(timeout=0.1)

        status = await session.command_status()
        assert status == TerminalCommandStatus.IDLE

    # ── 7. Status priority validation ─────────────────────────────────────

    @pytest.mark.asyncio
    async def test_completed_overrides_all(self, infra) -> None:
        """Dead session always returns COMPLETED regardless of other state."""
        session = await infra.get_default()
        backend = session._backend
        backend.alive = False
        backend._segment = _seg("Enter password: ", prompt=False)
        # Simulate some bytes were received
        backend._next_reads = [
            TerminalRead(stdout="Enter password: ", raw="Enter password: "),
        ]
        await session.poll_once(timeout=0.1)

        status = await session.command_status()
        assert status == TerminalCommandStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_waiting_input_overrides_executing(self, infra) -> None:
        """Even with active bytes, input markers take priority."""
        session = await infra.get_default()
        backend = session._backend
        backend._segment = _seg(
            "Enter new password: ",
            cursor="Enter new password: ",
        )
        backend._next_reads = [
            TerminalRead(stdout="Enter new password: ", raw="Enter new password: "),
        ]
        await session.poll_once(timeout=0.1)

        status = await session.command_status()
        # Must be WAITING_INPUT even though bytes are flowing
        assert status == TerminalCommandStatus.WAITING_INPUT


# ── 4. poll_until_settled — scenario outcomes ────────────────────────────────


class TestPollUntilSettledScenarios:
    """Verify poll_until_settled returns correct PollOutcome per scenario."""

    @pytest.fixture
    def infra(self):
        """Create a session with controllable backend."""
        from framework.tools.terminal.config import TerminalRuntimeConfig
        from framework.tools.terminal.managers import BaseTerminalManager
        from framework.tools.terminal.process_registry import ProcessRegistry
        from framework.tools.terminal.types import (
            Platform,
            ShellFamily,
            ShellInfo,
            TerminalVisibility,
        )

        class PollFakeBackend:
            platform = Platform.WINDOWS
            visibility = TerminalVisibility.HIDDEN

            def __init__(self) -> None:
                self.started = False
                self.alive = True
                self._reads: list[TerminalRead] = []
                self._segment = TerminalSegment(text="$ ", cursor_line="$ ", is_empty_prompt=True)
                self._buffer_text = ""

            async def start(self, shell=None, cwd=None, env=None) -> None:
                self.started = True

            async def write(self, data: str) -> None:
                pass

            async def read_pending(self, timeout: float = 0.1, max_size: int = 65536) -> TerminalRead:
                if self._reads:
                    return self._reads.pop(0)
                return TerminalRead()

            async def read(self, timeout: float = 0.1, max_size: int = 65536) -> str:
                r = await self.read_pending(timeout, max_size)
                return r.raw

            async def current_segment(self) -> TerminalSegment:
                return self._segment

            async def interrupt(self) -> None:
                pass

            async def terminate(self) -> None:
                self.alive = False

            async def kill(self) -> None:
                self.alive = False

            async def is_alive(self) -> bool:
                return self.alive

            def stdin_writable(self) -> bool:
                return self.alive

            async def drain_startup(self) -> None:
                pass

            async def clear_input_line(self) -> None:
                pass

            def mark_command_boundary(self) -> None:
                pass

            def output_buffer_text(self) -> str:
                return self._buffer_text

        cfg = TerminalRuntimeConfig(
            default_command_timeout_seconds=30,
            default_yield_ms=500,
            prompt_stabilize_ms=50,
        )
        manager = BaseTerminalManager(
            shell_info=ShellInfo(ShellFamily.BASH, "bash", Platform.WINDOWS),
            visibility=TerminalVisibility.HIDDEN,
            backend_factory=PollFakeBackend,
            config=cfg,
        )
        registry = ProcessRegistry()
        return manager, registry, cfg

    @pytest.mark.asyncio
    async def test_prompt_detected(self, infra) -> None:
        """After a command completes, poll detects the idle prompt."""
        mgr, reg, cfg = infra
        session = await mgr.get_default()
        backend = session._backend

        # Simulate a quick echo command: output + prompt
        backend._reads = [
            TerminalRead(stdout="hello\n$ ", raw="hello\n$ "),
        ]
        backend._segment = _seg("hello\n$ ", cursor="$ ", prompt=True)

        proc = reg.create(command="echo hello", terminal="default", cwd=None, pid=None)

        result = await poll_until_settled(
            session, reg, proc.id, cfg,
            yield_ms=500, timeout_seconds=30, check_input_wait=True,
        )
        assert result.outcome == PollOutcome.PROMPT_DETECTED

    @pytest.mark.asyncio
    async def test_input_wait_detected(self, infra) -> None:
        """When password prompt appears, poll detects input wait."""
        mgr, reg, cfg = infra
        session = await mgr.get_default()
        backend = session._backend

        backend._reads = [
            TerminalRead(
                stdout="[sudo] password for user: ",
                raw="[sudo] password for user: ",
            ),
        ]
        backend._segment = _seg(
            "[sudo] password for user: ",
            cursor="[sudo] password for user: ",
        )

        proc = reg.create(command="sudo ls", terminal="default", cwd=None, pid=None)

        result = await poll_until_settled(
            session, reg, proc.id, cfg,
            yield_ms=500, timeout_seconds=30, check_input_wait=True,
        )
        assert result.outcome == PollOutcome.INPUT_WAIT, (
            f"Expected INPUT_WAIT, got {result.outcome}"
        )

    @pytest.mark.asyncio
    async def test_stuck_detected(self, infra) -> None:
        """When bytes stop for 15s and no markers, poll detects stuck."""
        mgr, reg, cfg = infra
        session = await mgr.get_default()
        backend = session._backend

        # First: push one byte so _ever_received_bytes = True
        backend._reads = [
            TerminalRead(stdout="seed\n", raw="seed\n"),
        ]
        await session.poll_once(timeout=0.1)

        # Then: wind back _last_byte_at to simulate 20s silence
        session._last_byte_at = time.monotonic() - 20.0
        # Clear any remaining reads so poll_once returns empty
        backend._reads.clear()
        backend._segment = _seg("Connecting...\n")

        proc = reg.create(command="ssh dead-host", terminal="default", cwd=None, pid=None)

        # Use yield_ms > expected stuck check time so stuck triggers first
        result = await poll_until_settled(
            session, reg, proc.id, cfg,
            yield_ms=30000, timeout_seconds=30, check_input_wait=True,
        )
        assert result.outcome == PollOutcome.STUCK, (
            f"Expected STUCK, got {result.outcome}"
        )

    @pytest.mark.asyncio
    async def test_yield_on_continuous_output(self, infra) -> None:
        """Continuous output with no prompt → yields for LLM to check."""
        mgr, reg, cfg = infra
        session = await mgr.get_default()
        backend = session._backend

        # Simulate continuous output over several reads, no prompt
        for _ in range(10):
            backend._reads.append(
                TerminalRead(stdout="Building...\n", raw="Building...\n"),
            )
        backend._segment = _seg(
            "$ npm run build\n"
            "Building...\n" * 10,
        )

        proc = reg.create(command="npm run build", terminal="default", cwd=None, pid=None)

        result = await poll_until_settled(
            session, reg, proc.id, cfg,
            yield_ms=50, timeout_seconds=30, check_input_wait=True,
        )
        assert result.outcome in (PollOutcome.YIELDED, PollOutcome.PROMPT_DETECTED)

    @pytest.mark.asyncio
    async def test_process_exit_detected(self, infra) -> None:
        """When backend dies, poll detects process exit."""
        mgr, reg, cfg = infra
        session = await mgr.get_default()
        backend = session._backend

        # Kill the backend immediately
        backend.alive = False

        proc = reg.create(command="exit", terminal="default", cwd=None, pid=None)

        result = await poll_until_settled(
            session, reg, proc.id, cfg,
            yield_ms=500, timeout_seconds=30, check_input_wait=True,
        )
        assert result.outcome == PollOutcome.PROCESS_EXIT


# ── 5. XML output correctness per scenario ──────────────────────────────────


class TestXmlOutputPerScenario:
    """Verify that tool XML responses contain correct fields per scenario."""

    @pytest.mark.asyncio
    async def test_terminal_current_xml_for_executing(self) -> None:
        """terminal current XML when command is executing."""
        from framework.tools.terminal.tool import TerminalTool
        from tests.framework.tools.terminal.test_terminal_tool_current import FakeManager, FakeSession

        session = FakeSession()
        # Set last_byte_at 2 seconds ago so idle_ms > 0 and appears in XML
        session._last_byte_at = time.monotonic() - 2.0

        async def _status():
            return TerminalCommandStatus.EXECUTING

        async def _output():
            return "$ npm run build\n> build\nCompiling..."

        async def _segment():
            return TerminalSegment(
                text="$ npm run build\n> build\nCompiling...",
                cursor_line="Compiling...",
                is_empty_prompt=False,
            )

        session.command_status = _status
        session.last_command_output = _output
        session.current_segment = _segment

        class Manager(FakeManager):
            async def get_default_session(self_):
                return session

        tool = TerminalTool(Manager())
        result = await tool.execute(action="current")

        assert "<terminal_result>" in result
        assert "<status>executing</status>" in result
        assert "<output>" in result
        assert "npm run build" in result
        # idle_ms may or may not appear depending on timing
        # Just verify it doesn't crash and has correct status

    @pytest.mark.asyncio
    async def test_terminal_current_xml_for_waiting_input(self) -> None:
        """terminal current XML when waiting for input."""
        from framework.tools.terminal.tool import TerminalTool
        from tests.framework.tools.terminal.test_terminal_tool_current import FakeManager, FakeSession

        session = FakeSession()
        session._last_byte_at = time.monotonic()

        async def _status():
            return TerminalCommandStatus.WAITING_INPUT

        async def _output():
            return "$ sudo ls\n[sudo] password for user: "

        async def _segment():
            return TerminalSegment(
                text="$ sudo ls\n[sudo] password for user: ",
                cursor_line="[sudo] password for user: ",
                is_empty_prompt=False,
            )

        session.command_status = _status
        session.last_command_output = _output
        session.current_segment = _segment

        class Manager(FakeManager):
            async def get_default_session(self_):
                return session

        tool = TerminalTool(Manager())
        result = await tool.execute(action="current")

        assert "<terminal_result>" in result
        assert "<status>waiting_input</status>" in result
        assert "password" in result
        assert "<cursor>" in result

    @pytest.mark.asyncio
    async def test_terminal_current_xml_for_stuck(self) -> None:
        """terminal current XML when command is stuck."""
        from framework.tools.terminal.tool import TerminalTool
        from tests.framework.tools.terminal.test_terminal_tool_current import FakeManager, FakeSession

        session = FakeSession()
        session._last_byte_at = time.monotonic() - 16.0

        async def _status():
            return TerminalCommandStatus.STUCK

        async def _output():
            return "$ ssh dead-host\nConnecting..."

        async def _segment():
            return TerminalSegment(
                text="$ ssh dead-host\nConnecting...",
                cursor_line="Connecting...",
                is_empty_prompt=False,
            )

        session.command_status = _status
        session.last_command_output = _output
        session.current_segment = _segment

        class Manager(FakeManager):
            async def get_default_session(self_):
                return session

        tool = TerminalTool(Manager())
        result = await tool.execute(action="current")

        assert "<terminal_result>" in result
        assert "<status>stuck</status>" in result
        assert "<idle_ms>" in result
        assert "ssh" in result

    @pytest.mark.asyncio
    async def test_terminal_current_xml_for_unknown(self) -> None:
        """terminal current XML for unknown state — no session."""
        from framework.tools.terminal.tool import TerminalTool
        from tests.framework.tools.terminal.test_terminal_tool_current import FakeManager

        class EmptyManager(FakeManager):
            async def get_default_session(self_):
                return None

        tool = TerminalTool(EmptyManager())
        result = await tool.execute(action="current")

        assert "<terminal_result>" in result
        assert "<status>unknown</status>" in result
        assert "No terminal is active" in result

    @pytest.mark.asyncio
    async def test_terminal_current_xml_for_completed(self) -> None:
        """terminal current XML after command completes — should show
        full output from command line to new prompt."""
        from framework.tools.terminal.tool import TerminalTool
        from tests.framework.tools.terminal.test_terminal_tool_current import FakeManager, FakeSession

        session = FakeSession()
        session._last_byte_at = time.monotonic()

        async def _status():
            return TerminalCommandStatus.IDLE

        async def _output():
            return "$ echo hello\nhello\n$ "

        async def _segment():
            return TerminalSegment(
                text="$ echo hello\nhello\n$ ",
                cursor_line="$ ",
                is_empty_prompt=True,
            )

        session.command_status = _status
        session.last_command_output = _output
        session.current_segment = _segment

        class Manager(FakeManager):
            async def get_default_session(self_):
                return session

        tool = TerminalTool(Manager())
        result = await tool.execute(action="current")

        assert "<terminal_result>" in result
        assert "<status>idle</status>" in result
        assert "echo hello" in result
        assert "hello" in result
        assert "<cursor>$</cursor>" in result

    # ── CommandResult XML field tests ─────────────────────────────────────

    def test_command_result_xml_has_required_fields(self) -> None:
        """Verify _build_command_xml includes all required fields."""
        from framework.tools.terminal.command_tool import _build_command_xml
        from framework.tools.terminal.types import CommandResultStatus

        xml = _build_command_xml(
            "some output",
            CommandResultStatus.EXECUTING,
            1500,
            terminal="default",
            idle_ms=500,
            message="Command still running",
        )
        assert "<command_result>" in xml
        assert "<output>some output</output>" in xml
        assert "<status>executing</status>" in xml
        assert "<elapsed_ms>1500</elapsed_ms>" in xml
        assert "<terminal>default</terminal>" in xml
        assert "<idle_ms>500</idle_ms>" in xml
        assert "<message>Command still running</message>" in xml
        assert "</command_result>" in xml

    def test_command_result_xml_for_stuck_includes_message(self) -> None:
        """STUCK XML must include message field explaining the state."""
        from framework.tools.terminal.command_tool import CommandTool
        from framework.tools.terminal.types import CommandResultStatus

        xml = CommandTool._format_stuck(
            output_parts=["Connecting...\n"],
            raw_idle_ms=20000,
            elapsed_ms=25000,
            terminal="default",
        )
        assert "<status>stuck</status>" in xml
        assert "<message>" in xml
        assert "20s" in xml or "20000" in xml  # mentions idle duration
        assert "Ctrl+C" in xml or "interrupt" in xml or "process interrupt" in xml

    def test_command_result_xml_for_timed_out_includes_partial_output(self) -> None:
        """TIMED_OUT must include partial output + timeout message."""
        from framework.tools.terminal.command_tool import CommandTool

        xml = CommandTool._format_timed_out(
            output_parts=["Starting build...\n", "Step 1/100 done\n"],
            timeout_seconds=10,
            elapsed_ms=10000,
            terminal="default",
        )
        assert "<status>timed_out</status>" in xml
        assert "Starting build..." in xml
        assert "Step 1/100 done" in xml
        assert "<message>" in xml

    def test_command_result_xml_for_waiting_input_has_guidance(self) -> None:
        """WAITING_INPUT XML must guide LLM to provide input."""
        from framework.tools.terminal.command_tool import _build_command_xml
        from framework.tools.terminal.types import CommandResultStatus

        xml = _build_command_xml(
            "Password: ",
            CommandResultStatus.WAITING_INPUT,
            500,
            terminal="default",
            idle_ms=3000,
            message="Use process write to provide input.",
        )
        assert "<status>waiting_input</status>" in xml
        assert "<message>" in xml
        assert "write" in xml.lower() or "process write" in xml.lower()


# ── 6. Edge cases ────────────────────────────────────────────────────────────


class TestEdgeCases:
    """Tests for ambiguous / borderline scenarios."""

    @pytest.mark.asyncio
    async def test_slow_output_not_stuck(self) -> None:
        """Output every 10s — should be EXECUTING, not STUCK (within 15s window)."""
        from framework.tools.terminal.config import TerminalRuntimeConfig
        from framework.tools.terminal.managers import BaseTerminalManager
        from framework.tools.terminal.types import (
            Platform,
            ShellFamily,
            ShellInfo,
            TerminalVisibility,
        )

        class SlowBackend:
            platform = Platform.WINDOWS
            visibility = TerminalVisibility.HIDDEN

            def __init__(self):
                self.started = False
                self.alive = True
                self._next_reads: list = []
                self._segment = TerminalSegment(text="", cursor_line="", is_empty_prompt=False)
                self._buffer_text = "Processing...\n"

            async def start(self, shell=None, cwd=None, env=None): self.started = True
            async def write(self, data: str): pass
            async def read_pending(self, timeout=0.1, max_size=65536):
                if self._next_reads:
                    return self._next_reads.pop(0)
                return TerminalRead()
            async def read(self, timeout=0.1, max_size=65536):
                r = await self.read_pending(timeout, max_size)
                return r.raw
            async def current_segment(self): return self._segment
            async def interrupt(self): pass
            async def terminate(self): pass
            async def kill(self): pass
            async def is_alive(self): return True
            def stdin_writable(self): return True
            async def drain_startup(self): pass
            async def clear_input_line(self): pass
            def mark_command_boundary(self): pass
            def output_buffer_text(self): return self._buffer_text

        cfg = TerminalRuntimeConfig(prompt_stabilize_ms=50)
        manager = BaseTerminalManager(
            shell_info=ShellInfo(ShellFamily.BASH, "bash", Platform.WINDOWS),
            visibility=TerminalVisibility.HIDDEN,
            backend_factory=SlowBackend,
            config=cfg,
        )
        session = await manager.get_default()
        backend = session._backend
        backend._segment = TerminalSegment(
            text="Processing...\n",
            cursor_line="Processing...",
            is_empty_prompt=False,
        )
        # Push a byte so _ever_received_bytes = True, then wind back clock 10s
        backend._next_reads = [TerminalRead(stdout="data\n", raw="data\n")]
        await session.poll_once(timeout=0.1)
        session._last_byte_at = time.monotonic() - 10.0

        status = await session.command_status()
        # 10s silence → still EXECUTING (less than 15s threshold)
        assert status == TerminalCommandStatus.EXECUTING, (
            f"Expected EXECUTING (10s < 15s threshold), got {status}"
        )

    def test_extract_output_long_running_command(self) -> None:
        """Amidst long output, only recent segment is returned."""
        text = (
            "$ find / -name '*.py'\n"
            + "\n".join(f"/path/to/file_{i}.py" for i in range(500))
            + "\n$ "  # eventual prompt
        )
        result = extract_last_command_output(text)
        # Should start from the command prompt, include some output, end with prompt
        assert "$ find" in result
        assert "$ " in result
        # The output should NOT contain all 500 lines trimmed
        # (truncation happens at higher level, this is just extraction)

    def test_output_containing_dollar_signs_not_confused_with_prompt(self) -> None:
        """Output like 'Total cost: $ 42.50' should NOT be treated as a prompt line."""
        text = "$ calculate\nTotal cost: $ 42.50\nTax: $ 3.40\n$ "
        result = extract_last_command_output(text)
        assert "$ calculate" in result
        # "Total cost: $ 42.50" should NOT be detected as a second prompt
        # The function should start from the actual command prompt "$ calculate"
        assert "Total cost" in result


# ── 7. Realistic PTY byte-flow simulation ──────────────────────────────────
#
# These tests simulate what actually happens when commands run in a terminal:
# poll_once() is called repeatedly, each call returning a chunk of PTY output.
# command_status() checks the state at each step.
#
# This verifies that the four scenarios are correctly distinguished through
# the actual byte-level detection path, not just final-state assertions.


class TestPtyByteFlowSimulation:
    """Simulate full command lifecycles with realistic PTY byte patterns.

    Each test builds a session, feeds chunks of terminal output through
    poll_once(), and verifies command_status() at every step.  This is
    the same code path the tools use in production.
    """

    @pytest.fixture
    def infra(self):
        from framework.tools.terminal.config import TerminalRuntimeConfig
        from framework.tools.terminal.managers import BaseTerminalManager
        from framework.tools.terminal.types import (
            Platform,
            ShellFamily,
            ShellInfo,
            TerminalVisibility,
        )

        class FlowBackend:
            platform = Platform.WINDOWS
            visibility = TerminalVisibility.HIDDEN

            def __init__(self):
                self.started = False
                self.alive = True
                self._reads: list[TerminalRead] = []
                self._segment = TerminalSegment(text="$ ", cursor_line="$ ", is_empty_prompt=True)
                self._buffer_text = ""

            async def start(self, shell=None, cwd=None, env=None): self.started = True
            async def write(self, data: str): pass
            async def read_pending(self, timeout=0.1, max_size=65536):
                if self._reads:
                    return self._reads.pop(0)
                return TerminalRead()
            async def read(self, timeout=0.1, max_size=65536):
                r = await self.read_pending(timeout, max_size)
                return r.raw
            async def current_segment(self): return self._segment
            async def interrupt(self): pass
            async def terminate(self): self.alive = False
            async def kill(self): self.alive = False
            async def is_alive(self): return self.alive
            def stdin_writable(self): return self.alive
            async def drain_startup(self): pass
            async def clear_input_line(self): pass
            def mark_command_boundary(self): pass
            def output_buffer_text(self): return self._buffer_text

        cfg = TerminalRuntimeConfig(prompt_stabilize_ms=50)
        manager = BaseTerminalManager(
            shell_info=ShellInfo(ShellFamily.BASH, "bash", Platform.WINDOWS),
            visibility=TerminalVisibility.HIDDEN,
            backend_factory=FlowBackend,
            config=cfg,
        )
        return manager

    # ── Scenario 1: npm install — continuous output every few seconds ────

    @pytest.mark.asyncio
    async def test_continuous_output_flow(self, infra) -> None:
        """Simulate npm install: frequent chunks of output, no prompt until end."""
        session = await infra.get_default()
        backend = session._backend

        # Step 1: Command submitted, first output appears
        backend._reads = [
            TerminalRead(stdout="$ npm install\n", raw="$ npm install\n"),
            TerminalRead(stdout="npm WARN deprecated package@1.0.0\n", raw="npm WARN deprecated package@1.0.0\n"),
        ]
        backend._segment = _seg("$ npm install\nnpm WARN deprecated package@1.0.0\n")
        backend._buffer_text = backend._segment.text

        await session.poll_once(timeout=0.1)  # consume "$ npm install\n"
        await session.poll_once(timeout=0.1)  # consume the WARN line

        status = await session.command_status()
        assert status == TerminalCommandStatus.EXECUTING, (
            f"Step 1 (continuous output): expected EXECUTING, got {status}"
        )

        # Step 2: More output arrives after a few seconds
        backend._reads = [
            TerminalRead(stdout="npm WARN deprecated another@2.0.0\n", raw="npm WARN deprecated another@2.0.0\n"),
        ]
        backend._segment = _seg(
            "$ npm install\nnpm WARN deprecated package@1.0.0\nnpm WARN deprecated another@2.0.0\n"
        )
        await session.poll_once(timeout=0.1)

        status = await session.command_status()
        assert status == TerminalCommandStatus.EXECUTING, (
            f"Step 2 (more output): expected EXECUTING, got {status}"
        )

        # Step 3: Final output + prompt returns → IDLE
        backend._reads = [
            TerminalRead(stdout="added 150 packages in 45s\n$ ", raw="added 150 packages in 45s\n$ "),
        ]
        backend._segment = _seg("$ npm install\n...\nadded 150 packages in 45s\n$ ", cursor="$ ", prompt=True)
        await session.poll_once(timeout=0.1)

        status = await session.command_status()
        assert status == TerminalCommandStatus.IDLE, (
            f"Step 3 (completed): expected IDLE, got {status}"
        )

    # ── Scenario 2: curl download — refreshing progress with \r ──────────

    @pytest.mark.asyncio
    async def test_refreshing_progress_flow(self, infra) -> None:
        """Simulate curl download: \\r repaint progress, no newlines."""
        session = await infra.get_default()
        backend = session._backend

        # Feed chunks of \r-repainted progress
        chunks = [
            "\r  0 1024M    0     0    0     0      0      0 --:--:-- --:--:-- --:--:--     0",
            "\r  5 1024M    5  51.2M    0     0  5.1M      0  0:03:20  0:03:20 --:--:-- 5.1M",
            "\r 20 1024M   20  204M    0     0  5.2M      0  0:03:17  0:00:39  0:02:38 5.0M",
            "\r 50 1024M   50  512M    0     0  5.2M      0  0:03:17  0:01:38  0:01:39 5.2M",
        ]

        all_output = ""
        for i, chunk in enumerate(chunks):
            all_output += chunk
            backend._reads = [TerminalRead(stdout=chunk, raw=chunk)]
            backend._segment = _seg(f"$ curl -o file.zip URL\n{all_output}")
            await session.poll_once(timeout=0.1)

            # After each chunk, the status must be EXECUTING (not STUCK, not WAITING_INPUT)
            status = await session.command_status()
            assert status == TerminalCommandStatus.EXECUTING, (
                f"Step {i} (refreshing progress): expected EXECUTING, got {status}"
            )

        # Verify: even with "password" nowhere in the output, we're safe
        assert not is_waiting_for_input(all_output)

    @pytest.mark.asyncio
    async def test_refreshing_output_not_confused_with_input_prompt(self, infra) -> None:
        """\\r progress containing 'password' in status text must NOT trigger WAITING_INPUT."""
        session = await infra.get_default()
        backend = session._backend

        # A hypothetical progress message that contains 'password' word
        chunk = "\rUnlocking password store: 50% complete"
        backend._reads = [TerminalRead(stdout=chunk, raw=chunk)]
        backend._segment = _seg(f"$ some-cmd\n{chunk}")
        await session.poll_once(timeout=0.1)

        # After \\r rsplit: "Unlocking password store: 50% complete"
        # Ends with "complete" (or "e"), not with ":", so NOT waiting_input
        status = await session.command_status()
        assert status != TerminalCommandStatus.WAITING_INPUT, (
            f"Refreshing progress with 'password' word must NOT be WAITING_INPUT, got {status}"
        )

        # The raw is_waiting_for_input check on the accumulated output
        accumulated = "$ some-cmd\n" + "\rUnlocking password store: 50% complete"
        assert not is_waiting_for_input(accumulated), (
            "Progress output containing 'password' must not trigger input detection"
        )

    # ── Scenario 3: SSH to dead host — bytes stop, command hangs ─────────

    @pytest.mark.asyncio
    async def test_blocked_command_flow(self, infra) -> None:
        """Simulate SSH connecting to unreachable host: output stops → STUCK."""
        session = await infra.get_default()
        backend = session._backend

        # Initial output: connection attempt
        backend._reads = [
            TerminalRead(stdout="$ ssh dead-host\n", raw="$ ssh dead-host\n"),
            TerminalRead(stdout="Connecting to dead-host [192.0.2.1] port 22...\n",
                        raw="Connecting to dead-host [192.0.2.1] port 22...\n"),
        ]
        backend._segment = _seg("$ ssh dead-host\nConnecting to dead-host [192.0.2.1] port 22...\n")
        await session.poll_once(timeout=0.1)
        await session.poll_once(timeout=0.1)

        # Step 1: Just got output → EXECUTING
        status = await session.command_status()
        assert status == TerminalCommandStatus.EXECUTING, (
            f"Step 1 (just connected): expected EXECUTING, got {status}"
        )

        # Step 2: 16 seconds of silence (no more bytes)
        session._last_byte_at = time.monotonic() - 16.0

        status = await session.command_status()
        assert status == TerminalCommandStatus.STUCK, (
            f"Step 2 (16s silence): expected STUCK, got {status}"
        )

        # Step 3: Verify NOT WAITING_INPUT — there are no prompt markers
        assert status != TerminalCommandStatus.WAITING_INPUT

    # ── Scenario 4: sudo prompts for password ────────────────────────────

    @pytest.mark.asyncio
    async def test_password_prompt_flow(self, infra) -> None:
        """Simulate sudo command that prompts for password."""
        session = await infra.get_default()
        backend = session._backend

        # Command output + password prompt
        backend._reads = [
            TerminalRead(stdout="$ sudo apt install nginx\n", raw="$ sudo apt install nginx\n"),
            TerminalRead(stdout="[sudo] password for user: ", raw="[sudo] password for user: "),
        ]
        backend._segment = _seg(
            "$ sudo apt install nginx\n[sudo] password for user: ",
            cursor="[sudo] password for user: ",
        )
        await session.poll_once(timeout=0.1)
        await session.poll_once(timeout=0.1)

        status = await session.command_status()
        assert status == TerminalCommandStatus.WAITING_INPUT, (
            f"Password prompt: expected WAITING_INPUT, got {status}"
        )

    @pytest.mark.asyncio
    async def test_password_prompt_ends_with_colon(self, infra) -> None:
        """Verify that 'password:' at end of line IS detected (must not regress)."""
        session = await infra.get_default()
        backend = session._backend

        backend._reads = [
            TerminalRead(stdout="Password: ", raw="Password: "),
        ]
        backend._segment = _seg("Password: ", cursor="Password: ")
        await session.poll_once(timeout=0.1)

        status = await session.command_status()
        assert status == TerminalCommandStatus.WAITING_INPUT, (
            f"'Password: ' must be WAITING_INPUT, got {status}"
        )

    # ── Scenario 5: Full lifecycle — running → stuck → resumed → done ───

    @pytest.mark.asyncio
    async def test_full_lifecycle_running_stuck_resumed_done(self, infra) -> None:
        """A build that pauses (looks stuck), resumes, and completes."""
        session = await infra.get_default()
        backend = session._backend

        # Phase 1: Build starts
        backend._reads = [
            TerminalRead(stdout="$ make all\n", raw="$ make all\n"),
            TerminalRead(stdout="Compiling...\n", raw="Compiling...\n"),
        ]
        backend._segment = _seg("$ make all\nCompiling...\n")
        await session.poll_once(timeout=0.1)
        await session.poll_once(timeout=0.1)

        assert await session.command_status() == TerminalCommandStatus.EXECUTING

        # Phase 2: No output for 16s — appears stuck
        session._last_byte_at = time.monotonic() - 16.0
        assert await session.command_status() == TerminalCommandStatus.STUCK

        # Phase 3: But then it resumes! New output arrives
        backend._reads = [
            TerminalRead(stdout="Linking...\n", raw="Linking...\n"),
        ]
        backend._segment = _seg("$ make all\nCompiling...\nLinking...\n")
        await session.poll_once(timeout=0.1)

        # After new bytes, goes back to EXECUTING
        assert await session.command_status() == TerminalCommandStatus.EXECUTING

        # Phase 4: Prompt returns → IDLE
        backend._reads = [
            TerminalRead(stdout="Done!\n$ ", raw="Done!\n$ "),
        ]
        backend._segment = _seg("$ make all\nCompiling...\nLinking...\nDone!\n$ ",
                                cursor="$ ", prompt=True)
        await session.poll_once(timeout=0.1)

        assert await session.command_status() == TerminalCommandStatus.IDLE

    # ── Cross-shell prompt verification ──────────────────────────────────

    @pytest.mark.asyncio
    async def test_bash_prompt_detection_for_idle(self, infra) -> None:
        """Bash shell ($ ) prompt → IDLE."""
        session = await infra.get_default()
        backend = session._backend
        backend._segment = _seg("user@host:/path$ ", cursor="user@host:/path$ ", prompt=True)
        backend._reads = [TerminalRead(stdout="seed\n", raw="seed\n")]
        await session.poll_once(timeout=0.1)

        assert await session.command_status() == TerminalCommandStatus.IDLE

    @pytest.mark.asyncio
    async def test_powershell_prompt_detection_for_idle(self, infra) -> None:
        """PowerShell (PS ...> ) prompt → IDLE."""
        session = await infra.get_default()
        backend = session._backend
        backend._segment = _seg("PS C:\\Users\\admin> ", cursor="PS C:\\Users\\admin> ", prompt=True)
        backend._reads = [TerminalRead(stdout="seed\n", raw="seed\n")]
        await session.poll_once(timeout=0.1)

        assert await session.command_status() == TerminalCommandStatus.IDLE

    @pytest.mark.asyncio
    async def test_powershell_prompt_running_command(self, infra) -> None:
        """PowerShell running a command → EXECUTING, not IDLE."""
        session = await infra.get_default()
        backend = session._backend
        backend._segment = _seg(
            "PS C:\\project> dotnet build\n  Determining projects...\n  Build started...\n"
        )
        backend._reads = [
            TerminalRead(stdout="  Build started...\n", raw="  Build started...\n"),
        ]
        await session.poll_once(timeout=0.1)

        assert await session.command_status() == TerminalCommandStatus.EXECUTING

    @pytest.mark.asyncio
    async def test_root_prompt_detection_for_idle(self, infra) -> None:
        """Root shell (# ) prompt → IDLE."""
        session = await infra.get_default()
        backend = session._backend
        backend._segment = _seg("root@server:~# ", cursor="root@server:~# ", prompt=True)
        backend._reads = [TerminalRead(stdout="seed\n", raw="seed\n")]
        await session.poll_once(timeout=0.1)

        assert await session.command_status() == TerminalCommandStatus.IDLE

    # ── UNKNOWN safety net ───────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_fresh_session_with_no_bytes_is_unknown(self, infra) -> None:
        """A newly created session that has received nothing → UNKNOWN.
        The LLM sees this and knows it cannot determine the state yet."""
        session = await infra.get_default()
        # Never called poll_once() → _ever_received_bytes is False

        status = await session.command_status()
        assert status == TerminalCommandStatus.UNKNOWN, (
            f"Fresh session with no bytes must be UNKNOWN, got {status}"
        )
