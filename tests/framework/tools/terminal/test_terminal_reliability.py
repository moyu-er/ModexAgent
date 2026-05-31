"""Tests for terminal reliability: tab identity, command submission, and output fidelity.

These tests expose cross-cutting issues in the terminal tool system that affect
both visible and invisible backends.  The session/command layer is shared, so
bugs here break all backends equally.

Categories:
  A. Tab identity — output must identify which tab was used
  B. Tab switching — select_default + command must target the correct session
  C. Silent fallback — dead backend must NOT silently redirect to a new tab
  D. Command submission — write sequence must be correct per shell family
"""

from __future__ import annotations

import asyncio

import pytest

from framework.tools.terminal.command_tool import CommandTool
from framework.tools.terminal.config import TerminalRuntimeConfig
from framework.tools.terminal.managers import BaseTerminalManager
from framework.tools.terminal.process_registry import ProcessRegistry
from framework.tools.terminal.process_tool import ProcessTool
from framework.tools.terminal.results import TerminalRead, TerminalSegment
from framework.tools.terminal.tool import TerminalTool
from framework.tools.terminal.types import (
    Platform,
    ShellFamily,
    ShellInfo,
    TerminalVisibility,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeBackend:
    """Minimal backend that records writes and returns pre-programmed reads.

    Supports delayed read delivery: _preread_buffer is held until the first
    write containing a newline (simulating command submission), then moved
    to _queue for reading.
    """

    platform = Platform.WINDOWS
    visibility = TerminalVisibility.HIDDEN

    def __init__(
        self,
        *,
        shell_family: ShellFamily = ShellFamily.BASH,
        alive: bool = True,
    ) -> None:
        self.started = False
        self.alive = alive
        self.writes: list[str] = []
        self._preread_buffer: list[TerminalRead] = []
        self._queue: list[TerminalRead] = []
        self._segment = TerminalSegment(text="$ ", cursor_line="$ ", is_empty_prompt=True)
        self._shell_family = shell_family
        self._drain_startup_called = False
        self._command_written = False

    async def start(self, shell=None, cwd=None, env=None) -> None:
        self.started = True

    async def write(self, data: str) -> None:
        self.writes.append(data)
        # Release preread buffer when a newline is written (command submission)
        if not self._command_written and "\n" in data:
            self._command_written = True
            self._queue.extend(self._preread_buffer)
            self._preread_buffer.clear()

    async def read_pending(self, timeout: float, max_size: int) -> TerminalRead:
        if self._queue:
            return self._queue.pop(0)
        await asyncio.sleep(0)
        return TerminalRead()

    async def current_segment(self) -> TerminalSegment:
        return self._segment

    async def interrupt(self) -> None:
        self.writes.append("\x03")

    async def terminate(self) -> None:
        self.alive = False

    async def kill(self) -> None:
        self.alive = False

    async def is_alive(self) -> bool:
        return self.alive

    def stdin_writable(self) -> bool:
        return self.alive

    async def drain_startup(self) -> None:
        self._drain_startup_called = True

    async def clear_input_line(self) -> None:
        self.writes.append("\x01\x0b")

    def mark_command_boundary(self) -> None:
        pass

    async def read(self, timeout: float, max_size: int) -> str:
        r = await self.read_pending(timeout, max_size)
        return r.raw


async def _manager_with_tabs(
    *names: str,
    shell_family: ShellFamily = ShellFamily.BASH,
    visibility: TerminalVisibility = TerminalVisibility.HIDDEN,
) -> BaseTerminalManager:
    """Create a manager and eagerly create named sessions."""

    def factory() -> FakeBackend:
        return FakeBackend(shell_family=shell_family)

    manager = BaseTerminalManager(
        shell_info=ShellInfo(shell_family, "bash", Platform.WINDOWS),
        visibility=visibility,
        backend_factory=factory,
        config=TerminalRuntimeConfig(default_yield_ms=10),
    )
    for name in names:
        await manager.get_or_create(name)
    return manager


def _make_tool_set(
    manager: BaseTerminalManager,
    config: TerminalRuntimeConfig | None = None,
) -> tuple[CommandTool, ProcessTool, TerminalTool, ProcessRegistry]:
    cfg = config or TerminalRuntimeConfig(default_yield_ms=10)
    registry = ProcessRegistry(config=cfg)
    ct = CommandTool(manager=manager, registry=registry, config=cfg)
    pt = ProcessTool(registry=registry, manager=manager, config=cfg)
    tt = TerminalTool(manager=manager)
    return ct, pt, tt, registry


# ===================================================================
# A. Tab identity — output must identify which tab was used
# ===================================================================


class TestCommandOutputIncludesTabIdentity:
    """command_result XML must tell the agent which terminal tab was used.

    Without this, the agent cannot verify it actually operated on the
    intended tab — especially after terminal select.
    """

    @pytest.mark.asyncio
    async def test_completed_result_includes_terminal_name(self) -> None:
        """Completed command output must include the terminal/tab name."""
        cfg = TerminalRuntimeConfig(
            default_yield_ms=60_000,  # high — let prompt detection fire first
            prompt_stabilize_ms=0,    # immediate prompt stabilization
        )
        manager = await _manager_with_tabs("default")
        ct, _, _, _ = _make_tool_set(manager, config=cfg)
        session = await manager.get_default()
        backend: FakeBackend = session._backend  # type: ignore[assignment]
        backend._preread_buffer = [TerminalRead(stdout="ok\n", raw="ok\n"), TerminalRead()]
        backend._segment = TerminalSegment(text="$ ", cursor_line="$ ", is_empty_prompt=True)

        result = await ct.execute(command="echo ok")

        assert "<command_result>" in result
        assert "<status>completed</status>" in result
        # BUG: current output has no terminal identity
        assert "<terminal>" in result or "<tab>" in result or "terminal=" in result

    @pytest.mark.asyncio
    async def test_running_result_includes_terminal_name(self) -> None:
        """Running command output must include the terminal/tab name."""
        cfg = TerminalRuntimeConfig(default_yield_ms=10)
        manager = await _manager_with_tabs("worker-1")
        ct, _, _, _ = _make_tool_set(manager, config=cfg)

        result = await ct.execute(command="npm run dev")

        assert "<status>running</status>" in result
        # Agent must know which tab the running command is on
        assert "<terminal>" in result or "<tab>" in result or "terminal=" in result

    @pytest.mark.asyncio
    async def test_timed_out_result_includes_terminal_name(self) -> None:
        """Timed-out command output must include the terminal/tab name."""
        cfg = TerminalRuntimeConfig(
            default_command_timeout_seconds=1,
            command_tool_outer_timeout_seconds=3,
            prompt_stabilize_ms=0,
            default_yield_ms=60_000,
        )
        manager = await _manager_with_tabs("build-tab")
        ct, _, _, _ = _make_tool_set(manager, config=cfg)
        session = await manager.get_default()
        backend: FakeBackend = session._backend  # type: ignore[assignment]
        backend._preread_buffer = [TerminalRead(stdout="partial\n", raw="partial\n")]
        backend._segment = TerminalSegment(text="...", cursor_line="...", is_empty_prompt=False)

        result = await ct.execute(command="build")

        assert "<status>timed_out</status>" in result
        assert "<terminal>" in result or "<tab>" in result or "terminal=" in result


class TestProcessOutputIncludesTabIdentity:
    """process_result XML must tell the agent which terminal tab is involved."""

    @pytest.mark.asyncio
    async def test_process_log_includes_terminal_name(self) -> None:
        manager = await _manager_with_tabs("deploy")
        _, pt, _, registry = _make_tool_set(manager)
        session = registry.create(command="deploy.sh", terminal="deploy", cwd=None, pid=1)
        registry.append_output(session.id, "stdout", "deploying...\n")

        result = await pt.execute(action="log")

        assert "<process_result>" in result
        # BUG: process output has session_id but no terminal name
        assert "terminal=" in result or "<terminal>" in result or "deploy" in result

    @pytest.mark.asyncio
    async def test_process_list_includes_terminal_names(self) -> None:
        manager = await _manager_with_tabs("tab-a", "tab-b")
        _, pt, _, registry = _make_tool_set(manager)
        registry.create(command="job-a", terminal="tab-a", cwd=None, pid=1)
        registry.create(command="job-b", terminal="tab-b", cwd=None, pid=2)

        result = await pt.execute(action="list")

        assert "<process_result>" in result
        assert "tab-a" in result
        assert "tab-b" in result


class TestTerminalCurrentIncludesTabIdentity:
    """terminal current output must identify which tab is shown."""

    @pytest.mark.asyncio
    async def test_current_includes_terminal_name(self) -> None:
        manager = await _manager_with_tabs("monitor")
        _, _, tt, _ = _make_tool_set(manager)

        result = await tt.execute(action="current")

        assert "<terminal_result>" in result
        # BUG: current output has no terminal name
        assert "<terminal>" in result or "<tab>" in result or "monitor" in result


# ===================================================================
# B. Tab switching — select + command targets the correct session
# ===================================================================


class TestTabSwitching:
    """select_default must reliably redirect subsequent tool calls."""

    @pytest.mark.asyncio
    async def test_select_then_command_targets_correct_session(self) -> None:
        """After terminal select, command must write to the selected tab's backend."""
        manager = await _manager_with_tabs("default", "worker")
        ct, _, _, _ = _make_tool_set(manager)

        await manager.select_default("worker")
        session = await manager.get_default()
        assert session.name == "worker"

        backend: FakeBackend = session._backend
        backend._queue = [TerminalRead(stdout="ok\n", raw="ok\n"), TerminalRead()]
        backend._segment = TerminalSegment(text="$ ", cursor_line="$ ", is_empty_prompt=True)

        result = await ct.execute(command="echo ok")

        # Verify the write went to worker's backend, not default's
        assert any("echo ok" in w for w in backend.writes)

    @pytest.mark.asyncio
    async def test_select_then_process_targets_correct_session(self) -> None:
        """After terminal select, process write must go to the selected tab."""
        manager = await _manager_with_tabs("default", "worker")
        _, pt, _, registry = _make_tool_set(manager)

        await manager.select_default("worker")
        registry.create(command="server", terminal="worker", cwd=None, pid=1)

        # FakeTerminal-like: get_default returns worker session
        session = await manager.get_default()
        assert session.name == "worker"

        result = await pt.execute(action="write", data="hello")

        assert "<process_result>" in result
        # The write should have gone to worker's backend
        backend: FakeBackend = session._backend
        assert any("hello" in w for w in backend.writes)

    @pytest.mark.asyncio
    async def test_select_back_and_forth(self) -> None:
        """Switching between tabs multiple times targets correctly each time."""
        manager = await _manager_with_tabs("tab-a", "tab-b")
        ct, _, _, _ = _make_tool_set(manager)

        # Switch to tab-b
        await manager.select_default("tab-b")
        session_b = await manager.get_default()
        assert session_b.name == "tab-b"

        # Switch back to tab-a
        await manager.select_default("tab-a")
        session_a = await manager.get_default()
        assert session_a.name == "tab-a"
        assert session_a is not session_b

    @pytest.mark.asyncio
    async def test_select_nonexistent_tab_raises(self) -> None:
        """Selecting a tab that doesn't exist must raise, not silently fail."""
        manager = await _manager_with_tabs("default")
        with pytest.raises(ValueError, match="does not exist"):
            await manager.select_default("ghost")


# ===================================================================
# C. Silent fallback — dead backend must NOT silently redirect
# ===================================================================


class TestSilentFallback:
    """When a backend dies, the system must not silently redirect to a new tab.

    Current behavior: get_default_session() sets _default_name = None when
    the backend is dead, and get_default() creates a brand-new "default"
    session.  The agent never learns that it's operating on a different tab.
    """

    @pytest.mark.asyncio
    async def test_dead_backend_get_default_session_returns_none(self) -> None:
        """get_default_session should return None when backend is dead."""
        manager = await _manager_with_tabs("my-tab")
        await manager.select_default("my-tab")

        session = await manager.get_default()
        # Simulate: backend was started, then died
        session._backend_started = True
        session._backend.alive = False

        result = await manager.get_default_session()
        # BUG: _default_name is silently cleared — agent never knows
        assert result is None
        assert manager._default_name is None

    @pytest.mark.asyncio
    async def test_get_default_creates_new_session_after_death(self) -> None:
        """After backend death, get_default silently creates a NEW session.

        This is the core bug: the agent thinks it's on 'my-tab' but
        actually gets a brand-new 'default' session.
        """
        manager = await _manager_with_tabs("my-tab")
        await manager.select_default("my-tab")

        session = await manager.get_default()
        session._backend_started = True
        session._backend.alive = False

        # get_default_session clears _default_name
        await manager.get_default_session()

        # get_default creates a brand new session because _default_name is None
        new_session = await manager.get_default()
        # BUG: this is a different session, not 'my-tab'
        assert new_session.name == "default"
        assert new_session is not session

    @pytest.mark.asyncio
    async def test_command_after_backend_death_reports_mismatch(self) -> None:
        """Command after backend death must indicate the actual tab used.

        If the backend died and a new session was silently created, the
        command output must at minimum show the actual tab name so the
        agent can detect the mismatch.
        """
        manager = await _manager_with_tabs("my-tab")
        cfg = TerminalRuntimeConfig(default_yield_ms=10)
        ct, _, _, _ = _make_tool_set(manager, config=cfg)

        await manager.select_default("my-tab")
        original_session = await manager.get_default()
        original_session._backend_started = True
        original_session._backend.alive = False

        # Trigger the silent fallback
        await manager.get_default_session()

        # Now execute a command — this goes to a new "default" tab
        result = await ct.execute(command="pwd")

        # The result MUST tell the agent it's on a different tab
        # BUG: current output has no tab identity, so agent can't detect this
        assert "<terminal>" in result or "<tab>" in result or "terminal=" in result


# ===================================================================
# D. Command submission — write sequence per shell family
# ===================================================================


class TestCommandSubmissionSequence:
    """submit_command must produce the correct byte sequence per shell type.

    The write sequence differs between readline shells (bash/zsh) and
    non-readline shells (cmd).  Mixing them up causes the 'command not
    showing' issue where the PTY receives wrong control sequences.
    """

    @pytest.mark.asyncio
    async def test_bash_submits_with_lf_ending(self) -> None:
        """Bash commands must end with \\n (LF), not \\r\\n."""
        manager = await _manager_with_tabs(
            "default", shell_family=ShellFamily.BASH,
        )
        session = await manager.get_default()
        backend: FakeBackend = session._backend

        await session.submit_command("echo hello")

        # The last write should contain the command + \n
        cmd_write = [w for w in backend.writes if "echo hello" in w]
        assert len(cmd_write) == 1
        assert cmd_write[0] == "echo hello\n"

    @pytest.mark.asyncio
    async def test_cmd_submits_with_crlf_ending(self) -> None:
        """CMD commands must end with \\r\\n (CRLF), not \\n."""
        manager = await _manager_with_tabs(
            "default", shell_family=ShellFamily.CMD,
        )
        session = await manager.get_default()
        backend: FakeBackend = session._backend

        await session.submit_command("dir")

        cmd_write = [w for w in backend.writes if "dir" in w]
        assert len(cmd_write) == 1
        assert cmd_write[0] == "dir\r\n"

    @pytest.mark.asyncio
    async def test_bash_submit_command_does_not_send_control_sequences(self) -> None:
        """submit_command must NOT send readline-specific control sequences.

        \\x01\\x0b (clear_input_line) was removed from submit_command because the
        caller already prevents commands in busy/timeout/waiting_input states and
        the timing window between \\x01\\x0b and the command \\n is unreliable across
        PTY implementations — on slower shells the \\n can be consumed as part of
        the control sequence, causing the command to hang until a manual Enter.
        """
        manager = await _manager_with_tabs(
            "default", shell_family=ShellFamily.BASH,
        )
        session = await manager.get_default()
        backend: FakeBackend = session._backend

        await session.submit_command("ls")

        assert "\x01\x0b" not in backend.writes
        assert any("ls\n" in w for w in backend.writes)

    @pytest.mark.asyncio
    async def test_cmd_does_not_send_readline_clear(self) -> None:
        """CMD submit_command must NOT send \\x01\\x0b (readline-specific)."""
        manager = await _manager_with_tabs(
            "default", shell_family=ShellFamily.CMD,
        )
        session = await manager.get_default()
        backend: FakeBackend = session._backend

        await session.submit_command("dir")

        # CMD doesn't use readline — no \x01\x0b should be sent
        assert "\x01\x0b" not in backend.writes

    @pytest.mark.asyncio
    async def test_powershell_does_not_send_readline_clear(self) -> None:
        """PowerShell submit_command must NOT send readline clear."""
        manager = await _manager_with_tabs(
            "default", shell_family=ShellFamily.POWERSHELL,
        )
        session = await manager.get_default()
        backend: FakeBackend = session._backend

        await session.submit_command("Get-Date")

        assert "\x01\x0b" not in backend.writes


class TestClearInputLinePerShell:
    """clear_input_line must only be called for readline-capable shells."""

    @pytest.mark.asyncio
    async def test_bash_clears_input_line(self) -> None:
        backend = FakeBackend(shell_family=ShellFamily.BASH)
        await backend.clear_input_line()
        assert "\x01\x0b" in backend.writes

    @pytest.mark.asyncio
    async def test_zsh_clears_input_line(self) -> None:
        backend = FakeBackend(shell_family=ShellFamily.ZSH)
        await backend.clear_input_line()
        assert "\x01\x0b" in backend.writes

    @pytest.mark.asyncio
    async def test_cmd_clear_is_noop(self) -> None:
        """CMD backend clear_input_line must be a no-op."""
        manager = await _manager_with_tabs(
            "default", shell_family=ShellFamily.CMD,
        )
        session = await manager.get_default()
        backend: FakeBackend = session._backend

        # CMD backend's clear_input_line should do nothing
        # (submit_command should not call it for non-readline shells)
        await session.submit_command("dir")

        for w in backend.writes:
            assert w != "\x01\x0b", "CMD should not receive readline clear"


# ===================================================================
# E. Visibility agnostic — same session layer for visible/invisible
# ===================================================================


class TestVisibilityAgnosticSessionLayer:
    """Session/command logic must work identically for visible and hidden.

    The session layer (TerminalSession, CommandTool, ProcessTool) sits
    above the backend.  These tests verify the session layer doesn't
    accidentally depend on visibility.
    """

    @pytest.mark.asyncio
    async def test_hidden_tab_identity_in_command_output(self) -> None:
        """Command output includes tab name for hidden terminals."""
        cfg = TerminalRuntimeConfig(default_yield_ms=60_000, prompt_stabilize_ms=0)
        manager = await _manager_with_tabs(
            "hidden-tab", visibility=TerminalVisibility.HIDDEN,
        )
        ct, _, _, _ = _make_tool_set(manager, config=cfg)
        session = await manager.get_default()
        backend: FakeBackend = session._backend  # type: ignore[assignment]
        backend._preread_buffer = [TerminalRead(stdout="done\n", raw="done\n"), TerminalRead()]
        backend._segment = TerminalSegment(text="$ ", cursor_line="$ ", is_empty_prompt=True)

        result = await ct.execute(command="ls")

        assert "<command_result>" in result
        assert "<terminal>" in result or "<tab>" in result or "terminal=" in result

    @pytest.mark.asyncio
    async def test_visible_tab_identity_in_command_output(self) -> None:
        """Command output includes tab name for visible terminals."""
        cfg = TerminalRuntimeConfig(default_yield_ms=60_000, prompt_stabilize_ms=0)
        manager = await _manager_with_tabs(
            "visible-tab", visibility=TerminalVisibility.VISIBLE,
        )
        ct, _, _, _ = _make_tool_set(manager, config=cfg)
        session = await manager.get_default()
        backend: FakeBackend = session._backend  # type: ignore[assignment]
        backend._preread_buffer = [TerminalRead(stdout="done\n", raw="done\n"), TerminalRead()]
        backend._segment = TerminalSegment(text="$ ", cursor_line="$ ", is_empty_prompt=True)

        result = await ct.execute(command="ls")

        assert "<command_result>" in result
        assert "<terminal>" in result or "<tab>" in result or "terminal=" in result

    @pytest.mark.asyncio
    async def test_both_visibilities_same_xml_structure(self) -> None:
        """Hidden and visible terminals produce identical XML structure."""
        results = []
        for vis in (TerminalVisibility.HIDDEN, TerminalVisibility.VISIBLE):
            manager = await _manager_with_tabs("tab", visibility=vis)
            ct, _, _, _ = _make_tool_set(manager)
            session = await manager.get_default()
            backend: FakeBackend = session._backend
            backend._queue = [TerminalRead(stdout="ok\n", raw="ok\n"), TerminalRead()]
            backend._segment = TerminalSegment(text="$ ", cursor_line="$ ", is_empty_prompt=True)
            results.append(await ct.execute(command="echo ok"))

        hidden, visible = results
        # Same top-level tags
        assert hidden.startswith("<command_result>")
        assert visible.startswith("<command_result>")
        # Both must have (or lack) terminal identity identically
        has_term_hidden = "<terminal>" in hidden or "<tab>" in hidden or "terminal=" in hidden
        has_term_visible = "<terminal>" in visible or "<tab>" in visible or "terminal=" in visible
        assert has_term_hidden == has_term_visible


# ===================================================================
# F. ShellFamily command_ending correctness
# ===================================================================


class TestShellFamilyCommandEnding:
    """command_ending() must return the correct terminator per shell family.

    Wrong ending → command sits in PTY input buffer unprocessed.
    """

    def test_bash_uses_lf(self) -> None:
        assert ShellFamily.BASH.command_ending() == "\n"

    def test_zsh_uses_lf(self) -> None:
        assert ShellFamily.ZSH.command_ending() == "\n"

    def test_sh_uses_lf(self) -> None:
        assert ShellFamily.SH.command_ending() == "\n"

    def test_cmd_uses_crlf(self) -> None:
        assert ShellFamily.CMD.command_ending() == "\r\n"

    def test_powershell_uses_crlf(self) -> None:
        assert ShellFamily.POWERSHELL.command_ending() == "\r\n"

    def test_readline_shells_are_bash_zsh_sh(self) -> None:
        """Only bash, zsh, sh should claim readline."""
        assert ShellFamily.BASH.uses_readline() is True
        assert ShellFamily.ZSH.uses_readline() is True
        assert ShellFamily.SH.uses_readline() is True
        assert ShellFamily.CMD.uses_readline() is False
        assert ShellFamily.POWERSHELL.uses_readline() is False
