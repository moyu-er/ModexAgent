"""POSIX terminal trinity e2e — real pexpect (HIDDEN) + tmux (VISIBLE).

Mirrors the three Windows workflow test files for macOS/Linux, combining
``test_windows_terminal_command_process_workflow.py`` (tab workflow + recreate),
``test_terminal_process_interaction.py`` (prompt shapes + pager), and
``test_terminal_send_keys_visible.py`` (send_keys byte delivery + kill) into
three high-value tests:

1. ``test_terminal_command_process_workflow`` — full tab workflow:
   open/switch/close, env-var inheritance, cd/pwd/export state persistence,
   interactive prompt via ProcessTool.write, long-running interrupt + recovery.

2. ``test_command_recreate_default_after_manual_close`` — manually kill the
   default terminal; next command must recreate it.

3. ``test_terminal_interactive_prompts`` — the five prompt shapes + multiline
   paste + interrupt recovery + pager scroll/quit + send_keys byte delivery
   (Ctrl-D/Ctrl-C/Ctrl-U) + process kill clearing the registry.

All three parametrize over visibility (HIDDEN=pexpect, VISIBLE=tmux+
Terminal.app) × shell (bash, zsh). All skip on Windows.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import subprocess
import sys
from xml.etree import ElementTree as ET

import pytest

from modex_agent.tools.terminal.command_tool import CommandTool
from modex_agent.tools.terminal.config import TerminalRuntimeConfig
from modex_agent.tools.terminal.managers import BaseTerminalManager, create_terminal_manager
from modex_agent.tools.terminal.process_registry import ProcessRegistry
from modex_agent.tools.terminal.process_tool import ProcessTool
from modex_agent.tools.terminal.tool import TerminalTool
from modex_agent.tools.terminal.types import (
    Platform,
    ShellFamily,
    ShellInfo,
    TerminalCommandStatus,
    TerminalVisibility,
)

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX-only (pexpect/tmux); Windows has its own suite"
)

_ENV_MARKER = "MODEX_TERMINAL_TEST_VAR"
_ENV_VALUE = "inherited-from-parent"


# ── helpers ───────────────────────────────────────────────────────


def _extract_output(xml_or_text: str) -> str:
    try:
        root = ET.fromstring(xml_or_text)
    except ET.ParseError:
        return xml_or_text
    return root.findtext("output", default=xml_or_text)


def _output_of(xml_or_text: str, marker: str) -> bool:
    text = _extract_output(xml_or_text)
    return any(line.strip() == marker for line in text.splitlines())


def _shell_platform() -> Platform:
    if sys.platform == "darwin":
        return Platform.DARWIN
    return Platform.LINUX


def _shell_param_id(value: object) -> str:
    return str(value)


def _make_cfg() -> TerminalRuntimeConfig:
    return TerminalRuntimeConfig(
        default_command_timeout_seconds=15,
        command_tool_outer_timeout_seconds=20,
        default_yield_ms=500,
        prompt_stabilize_ms=200,
        no_output_timeout_ms=8_000,
        input_wait_idle_ms=6_000,
    )


def _shells() -> list[tuple[str, str, ShellFamily]]:
    out: list[tuple[str, str, ShellFamily]] = []
    for name, family in [("bash", ShellFamily.BASH), ("zsh", ShellFamily.ZSH)]:
        path = shutil.which(name)
        if path:
            out.append((name, path, family))
    return out


def _has_pexpect() -> bool:
    if not any(shutil.which(s) for s in ("bash", "zsh")):
        return False
    try:
        import pexpect  # noqa: F401
    except ImportError:
        return False
    return True


def _has_visible_support() -> bool:
    if shutil.which("tmux") is None:
        return False
    try:
        import libtmux  # noqa: F401
    except ImportError:
        return False
    return any(shutil.which(em) is not None for em in ("osascript", "xterm", "gnome-terminal"))


def _kill_test_tmux_sessions() -> None:
    """Kill tmux sessions that look like test artifacts."""
    try:
        result = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return
    if result.returncode != 0:
        return
    for name in result.stdout.strip().splitlines():
        n = name.strip()
        if n.startswith(("agent_", "test_", "debug_")):
            subprocess.run(["tmux", "kill-session", "-t", n], timeout=2)


# ── bundle + fixtures ─────────────────────────────────────────────


class _Bundle:
    __slots__ = ("terminal", "command", "process", "manager", "registry", "cfg")

    def __init__(
        self,
        terminal: TerminalTool,
        command: CommandTool,
        process: ProcessTool,
        manager: BaseTerminalManager,
        registry: ProcessRegistry,
        cfg: TerminalRuntimeConfig,
    ) -> None:
        self.terminal = terminal
        self.command = command
        self.process = process
        self.manager = manager
        self.registry = registry
        self.cfg = cfg


@pytest.fixture(autouse=True)
def _env_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_ENV_MARKER, _ENV_VALUE)


def _make_bundle(visibility: TerminalVisibility, shell_path: str, family: ShellFamily) -> _Bundle:
    cfg = _make_cfg()
    shell_info = ShellInfo(family=family, path=shell_path, platform=_shell_platform())
    manager = create_terminal_manager(
        shell_info=shell_info,
        visibility=visibility,
        config=cfg,
    )
    registry = ProcessRegistry(config=cfg)
    return _Bundle(
        terminal=TerminalTool(manager),
        command=CommandTool(manager=manager, registry=registry, config=cfg),
        process=ProcessTool(registry=registry, manager=manager, config=cfg),
        manager=manager,
        registry=registry,
        cfg=cfg,
    )


async def _cleanup(bundle: _Bundle) -> None:
    for name in list(bundle.manager._sessions):
        with contextlib.suppress(TimeoutError, Exception):
            await asyncio.wait_for(bundle.manager.close(name), timeout=5.0)
    _kill_test_tmux_sessions()


async def _wait_for_status(
    bundle: _Bundle,
    targets: frozenset[TerminalCommandStatus],
    *,
    timeout: float = 8.0,
    interval: float = 0.2,
) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        session = await bundle.manager.get_default_session()
        if session is not None:
            status = await session.command_status(config=bundle.cfg)
            if status in targets:
                return
        await asyncio.sleep(interval)
    raise AssertionError(
        f"session did not reach {sorted(t.value for t in targets)} within {timeout}s"
    )


_IDLE = frozenset(
    {TerminalCommandStatus.IDLE, TerminalCommandStatus.UNKNOWN, TerminalCommandStatus.COMPLETED}
)


async def _wait_idle(bundle: _Bundle, timeout: float = 8.0) -> None:
    await _wait_for_status(bundle, _IDLE, timeout=timeout)


async def _wait_executing(bundle: _Bundle, timeout: float = 8.0) -> None:
    await _wait_for_status(
        bundle,
        frozenset({TerminalCommandStatus.EXECUTING, TerminalCommandStatus.WAITING_INPUT}),
        timeout=timeout,
    )


async def _start_interactive(bundle: _Bundle, command: str) -> str:
    """Run an interactive command; expect WAITING_INPUT or content marker."""
    res = await asyncio.wait_for(bundle.command.execute(command=command), timeout=20.0)
    lowered = res.lower()
    if "waiting_input" in lowered:
        return res
    if "executing" in lowered or "timed_out" in lowered:
        await _wait_for_status(
            bundle, frozenset({TerminalCommandStatus.WAITING_INPUT}), timeout=12.0
        )
        return res
    return res


async def _answer_and_verify(
    bundle: _Bundle,
    answer: str,
    expected_marker: str,
    *,
    negative_marker: str | None = None,
) -> str:
    write_res = await bundle.process.execute(action="write", data=answer, submit=True)
    assert "rejected" not in write_res.lower(), f"process write rejected:\n{write_res}"

    drained = write_res
    if not _output_of(drained, expected_marker):
        for _ in range(30):
            drained = await bundle.terminal.execute(action="current")
            if _output_of(drained, expected_marker):
                break
            await asyncio.sleep(0.3)

    assert _output_of(drained, expected_marker), (
        f"answer {answer!r} did not produce {expected_marker!r}:\n{drained}"
    )
    if negative_marker is not None:
        assert not _output_of(drained, negative_marker), (
            f"unexpected branch {negative_marker!r} after {answer!r}:\n{drained}"
        )
    return drained


# ── parametrization ───────────────────────────────────────────────


def _visibility_params() -> list[pytest.param]:
    params: list[pytest.param] = []
    if _has_pexpect():
        params.append(pytest.param(TerminalVisibility.HIDDEN, id="hidden"))
    if _has_visible_support():
        params.append(pytest.param(TerminalVisibility.VISIBLE, id="visible"))
    return params


def _shell_params() -> list[pytest.param]:
    return [pytest.param(name, path, family, id=name) for name, path, family in _shells()]


# ── Test 1: full terminal/command/process workflow ────────────────


@pytest.mark.parametrize(
    "shell_name, shell_path, shell_family", _shell_params(), ids=_shell_param_id
)
@pytest.mark.parametrize("visibility", _visibility_params())
@pytest.mark.timeout(150)
async def test_terminal_command_process_workflow(
    visibility: TerminalVisibility,
    shell_name: str,
    shell_path: str,
    shell_family: ShellFamily,
) -> None:
    """Full tab-switching, command, and process interaction on a real POSIX PTY.

    Mirrors ``test_terminal_command_process_workflow`` on Windows: open two
    tabs, prove env-var inheritance and cd/export state persistence, answer an
    interactive prompt via ProcessTool.write, interrupt a long-running command,
    verify recovery, then close all tabs.
    """
    bundle = _make_bundle(visibility, shell_path, shell_family)
    b = bundle
    try:
        # 1. Open tab-a.
        result = await b.terminal.execute(action="open", name="tab-a")
        assert "Opened" in result, f"open tab-a failed: {result}"

        # 2. Command in tab-a: env-var inheritance from parent process.
        result = await b.command.execute(command=f'echo "ENV=${{{_ENV_MARKER}}}"')
        assert _ENV_VALUE in _extract_output(result), f"env var not inherited:\n{result}"

        # 3. Open tab-b (auto-selected as default).
        result = await b.terminal.execute(action="open", name="tab-b")
        assert "Opened" in result, f"open tab-b failed: {result}"

        # 4. Command in tab-b.
        result = await b.command.execute(command='echo "TAB=tab-b"')
        assert "TAB=tab-b" in _extract_output(result), f"command in tab-b failed:\n{result}"

        # 5. Switch back to tab-a.
        result = await b.terminal.execute(action="select", name="tab-a")
        assert "Selected" in result, f"select tab-a failed: {result}"

        # 6. State persistence — cd + pwd.
        await b.command.execute(command="cd /tmp")
        result = await b.command.execute(command="pwd")
        assert "/tmp" in _extract_output(result), f"cwd not persisted:\n{result}"

        # 7. State persistence — export + echo.
        await b.command.execute(command="export MY_VAR=hello123")
        result = await b.command.execute(command='echo "$MY_VAR"')
        assert "hello123" in _extract_output(result), f"env var not persisted:\n{result}"

        # 8. Interactive prompt — read + ProcessTool.write.
        result = await b.command.execute(command='printf "name: "; read val; echo "got_$val"')
        if "waiting_input" not in result.lower():
            await _wait_for_status(
                b, frozenset({TerminalCommandStatus.WAITING_INPUT}), timeout=12.0
            )
        result = await b.process.execute(action="write", data="alice", submit=True)
        assert "got_alice" in result, f"process write did not answer:\n{result}"

        # 9. Long-running command — interrupt + recovery.
        sleep_task = asyncio.create_task(b.command.execute(command="sleep 60"))
        await _wait_executing(b, timeout=8.0)
        await b.process.execute(action="interrupt")
        await asyncio.sleep(1.0)
        sleep_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, BaseException):
            await sleep_task
        await b.process.execute(action="interrupt")
        await asyncio.sleep(1.0)
        sleep_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, BaseException):
            await sleep_task

        result = await b.command.execute(command='echo "RECOVERED"')
        assert "RECOVERED" in _extract_output(result), f"recovery failed:\n{result}"

        # 10. Close both tabs.
        result = await b.terminal.execute(action="close", name="tab-a")
        assert "Closed" in result, f"close tab-a failed: {result}"
        result = await b.terminal.execute(action="close", name="tab-b")
        assert "Closed" in result, f"close tab-b failed: {result}"

        # 11. List should be empty.
        result = await b.terminal.execute(action="list")
        assert "No active" in result, f"list not empty:\n{result}"

        # 12. VISIBLE mode: current reports visible.
        if visibility == TerminalVisibility.VISIBLE:
            await b.terminal.execute(action="open", name="vis-check")
            await b.command.execute(command="echo visible_ok")
            result = await b.terminal.execute(action="current")
            assert "visible" in result.lower(), f"current should report visible:\n{result}"
            await b.terminal.execute(action="close", name="vis-check")
    finally:
        await _cleanup(bundle)


# ── Test 2: default recreation after manual close ─────────────────


@pytest.mark.parametrize(
    "shell_name, shell_path, shell_family", _shell_params(), ids=_shell_param_id
)
@pytest.mark.parametrize("visibility", _visibility_params())
@pytest.mark.timeout(120)
async def test_command_recreate_default_after_manual_close(
    visibility: TerminalVisibility,
    shell_name: str,
    shell_path: str,
    shell_family: ShellFamily,
) -> None:
    """Manually killing the default terminal must not break the next command.

    Mirrors ``test_command_recreate_default_after_manual_close`` on Windows.
    """
    bundle = _make_bundle(visibility, shell_path, shell_family)
    b = bundle
    try:
        await b.terminal.execute(action="open", name="default")
        result = await b.command.execute(command='echo "before-close"')
        assert "before-close" in _extract_output(result), f"initial command failed:\n{result}"

        session = await b.manager.get_default_session()
        assert session is not None, "no default session"
        await session.terminate()
        for _ in range(20):
            if not await session.is_alive():
                break
            await asyncio.sleep(0.1)
        assert not await session.is_alive(), "session did not die after terminate()"

        result = await b.command.execute(command='echo "after-close"')
        assert "New terminal tab" in result or "after-close" in _extract_output(result), (
            f"expected recreate hint:\n{result}"
        )
        assert "after-close" in _extract_output(result), f"recreated tab command failed:\n{result}"

        await b.terminal.execute(action="close", name="default")
    finally:
        await _cleanup(bundle)


# ── Test 3: interactive prompts + send_keys + pager + kill ────────


@pytest.mark.parametrize(
    "shell_name, shell_path, shell_family", _shell_params(), ids=_shell_param_id
)
@pytest.mark.parametrize("visibility", _visibility_params())
@pytest.mark.timeout(180)
async def test_terminal_interactive_prompts(
    visibility: TerminalVisibility,
    shell_name: str,
    shell_path: str,
    shell_family: ShellFamily,
) -> None:
    """Five prompt shapes + multiline paste + interrupt + pager + send_keys + kill.

    Combines Windows ``test_hidden_process_interaction`` (prompt shapes + pager)
    and ``test_visible_send_keys_and_paste`` (send_keys byte delivery + process
    kill) into one pipeline that exercises terminal/command/process tool
    alternation throughout:

      1. Password via ``read -s`` — secret never echoed.
      2. ``y`` answer → yes branch.
      3. ``yes`` long answer → yes branch.
      4. ``n`` answer → no branch.
      5. ``no`` long answer → no branch.
      6. Multiline paste via ``cat > file`` — every line preserved.
      7. Interrupt recovery — sleep 60 + interrupt + next command works.
      8. Pager (``less``) — scroll with Space, dismiss with ``q``.
      9. send_keys Ctrl-D ends running ``cat``.
     10. send_keys Ctrl-C interrupts running ``sleep``.
     11. send_keys Ctrl-U clears partial readline input.
     12. Process kill clears the running registry.
    """
    bundle = _make_bundle(visibility, shell_path, shell_family)
    b = bundle
    try:
        # ── 1. Password (silent prompt — exercises idle-based detection) ──
        secret = "S3cret_pwd_7f1a"
        await _start_interactive(b, 'read -s v; echo; echo got_"$v"')
        await _answer_and_verify(b, secret, f"got_{secret}")

        recent = await b.terminal.execute(action="current")
        bare = [ln for ln in recent.splitlines() if ln.strip() == secret]
        assert not bare, f"password leaked:\n{recent}"

        # ── 2. y → yes branch ──────────────────────────────────────────
        await _start_interactive(
            b, 'printf "x: "; read a; case "$a" in y) echo B1;; *) echo B2;; esac'
        )
        await _answer_and_verify(b, "y", "B1", negative_marker="B2")

        # ── 3. yes → yes branch ────────────────────────────────────────
        await _start_interactive(
            b, 'printf "x: "; read a; case "$a" in y|yes|Y|YES) echo B1;; *) echo B2;; esac'
        )
        await _answer_and_verify(b, "yes", "B1", negative_marker="B2")

        # ── 4. n → no branch ───────────────────────────────────────────
        await _start_interactive(
            b, 'printf "x: "; read a; case "$a" in y) echo B1;; *) echo B2;; esac'
        )
        await _answer_and_verify(b, "n", "B2", negative_marker="B1")

        # ── 5. no → no branch ──────────────────────────────────────────
        await _start_interactive(
            b, 'printf "x: "; read a; case "$a" in y|yes|Y|YES) echo B1;; *) echo B2;; esac'
        )
        await _answer_and_verify(b, "no", "B2", negative_marker="B1")

        # ── 6. Multiline paste — every line preserved ──────────────────
        lines = ["line-one-7c1", "line-two-7c1", "line-three-7c1"]
        cat_task = asyncio.create_task(
            b.command.execute(command="cat > /tmp/modex_paste_test_7c1.txt")
        )
        await asyncio.sleep(2.0)
        for line in lines:
            await b.process.execute(action="write", data=line, submit=True)
            await asyncio.sleep(0.3)
        await asyncio.sleep(0.5)
        await b.process.execute(action="send_keys", hex=["04"])  # Ctrl-D EOF
        await asyncio.sleep(1.0)
        cat_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, BaseException):
            await cat_task
        await asyncio.sleep(0.5)
        await b.process.execute(action="interrupt")
        await asyncio.sleep(0.5)

        cat_out = await asyncio.wait_for(
            b.command.execute(command="cat /tmp/modex_paste_test_7c1.txt"), timeout=15.0
        )
        cat_text = _extract_output(cat_out)
        for line in lines:
            assert line in cat_text, f"pasted line {line!r} missing:\n{cat_out}"

        # ── 7. Interrupt recovery — sleep 60 + interrupt + next command ─
        sleep_task = asyncio.create_task(b.command.execute(command="sleep 60"))
        await _wait_executing(b, timeout=8.0)
        await b.process.execute(action="interrupt")
        await asyncio.sleep(1.0)
        sleep_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, BaseException):
            await sleep_task
        # Send a second interrupt to clear any residual executing state.
        await b.process.execute(action="interrupt")
        await asyncio.sleep(1.0)

        recover = "RECOVERED_8e2d"
        res = await asyncio.wait_for(b.command.execute(command=f"echo {recover}"), timeout=15.0)
        assert _output_of(res, recover), f"recovery failed:\n{res}"

        # ── 8. Pager — less entered, scrolled with Space, dismissed with q ─
        pager_task = asyncio.create_task(b.command.execute(command="seq 1 200 | less"))
        await asyncio.sleep(2.0)

        session = await b.manager.get_default_session()
        assert session is not None
        status = await session.command_status(config=b.cfg)
        assert status != TerminalCommandStatus.IDLE, f"less should be running, status={status}"

        await b.process.execute(action="write", data=" ", repeat=3, submit=False)
        await asyncio.sleep(0.5)
        # submit=False: 'q' must reach less without a trailing \r, which
        # would scroll instead of quitting.
        await b.process.execute(action="write", data="q", submit=False)
        await asyncio.sleep(1.0)
        pager_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, BaseException):
            await pager_task
        # less exit may leave the session in a transitional state; run a
        # trivial command to force prompt detection.
        with contextlib.suppress(Exception):
            await asyncio.wait_for(b.command.execute(command="echo pager_done"), timeout=15.0)

        # ── 9. send_keys Ctrl-D ends running cat ───────────────────────
        await b.terminal.execute(action="open", name="ctrl-d-test")
        cat2_task = asyncio.create_task(b.command.execute(command="cat"))
        await asyncio.sleep(2.0)
        await _wait_for_status(
            b,
            frozenset({TerminalCommandStatus.EXECUTING, TerminalCommandStatus.WAITING_INPUT}),
            timeout=6.0,
        )
        await b.process.execute(action="send_keys", hex=["04"])
        await _wait_idle(b, timeout=8.0)
        cat2_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, BaseException):
            await cat2_task

        # ── 10. send_keys Ctrl-C interrupts running sleep ──────────────
        await b.terminal.execute(action="open", name="ctrl-c-test")
        sleep2_task = asyncio.create_task(b.command.execute(command="sleep 60"))
        await _wait_executing(b, timeout=8.0)
        await b.process.execute(action="send_keys", hex=["03"])
        await _wait_idle(b, timeout=8.0)
        sleep2_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, BaseException):
            await sleep2_task

        # ── 11. send_keys Ctrl-U clears partial readline input ─────────
        # Only on byte-stream backends (HIDDEN/pexpect). tmux's capture_pane
        # reads the screen buffer, which doesn't reflect readline's internal
        # edit state until the line is submitted.
        if visibility == TerminalVisibility.HIDDEN:
            await b.terminal.execute(action="open", name="ctrl-u-test")
            await b.command.execute(command="echo warmup_9c4b")
            await _wait_idle(b, timeout=5.0)

            sess = await b.manager.get_default_session()
            assert sess is not None
            await sess.write("garbage_partial_zz")
            await asyncio.sleep(1.0)
            await b.process.execute(action="send_keys", hex=["15"])  # Ctrl-U
            await asyncio.sleep(0.8)
            seg = await sess.current_segment()
            assert "garbage_partial_zz" not in seg.cursor_line, (
                f"Ctrl-U did not clear readline input:\ncursor={seg.cursor_line!r}"
            )

        # ── 12. Process kill clears the running registry ───────────────
        for s in b.registry.list_running():
            await b.terminal.execute(action="select", name=s.terminal)
            await b.process.execute(action="kill")
        assert not b.registry.list_running(), "registry still has running sessions after kill"

        await b.terminal.execute(action="open", name="kill-test")
        await b.command.execute(command="sleep 60")
        running = b.registry.list_running()
        assert len(running) == 1, f"expected 1 running session, got {len(running)}: {running}"

        await b.process.execute(action="kill")
        assert not b.registry.list_running(), (
            "registry still has running sessions after process kill"
        )

        # Close all tabs we opened in this test.
        for name in ("ctrl-d-test", "ctrl-c-test", "ctrl-u-test", "kill-test"):
            with contextlib.suppress(Exception):
                await b.terminal.execute(action="close", name=name)
    finally:
        await _cleanup(bundle)
