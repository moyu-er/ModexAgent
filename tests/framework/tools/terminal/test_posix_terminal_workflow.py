"""POSIX terminal trinity e2e — real pexpect (HIDDEN) + tmux (VISIBLE).

Mirrors the Windows test structure (``test_windows_terminal_command_process_workflow.py``)
for macOS/Linux. Three high-value tests parametrized over visibility × shell:

1. ``test_posix_terminal_workflow`` — tab management + env inheritance +
   state persistence + interactive write + interrupt recovery + close.
2. ``test_posix_command_recreate_default_after_manual_close`` — manually
   kill default terminal; next command recreates it.
3. ``test_posix_full_capability_sample`` — a sample of terminal tab management,
   command state persistence, process writes, interrupt recovery, and close.

All tests skip on Windows (Windows has its own suite). VISIBLE skips when
tmux or terminal emulator is unavailable.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
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

pytestmark = [
    pytest.mark.skipif(
        sys.platform == "win32", reason="POSIX-only (pexpect/tmux); Windows has its own suite"
    ),
    # Real-PTY e2e: spawn shells + tmux; timing-sensitive under full-suite load.
    # Deselected by the default addopts (-m 'not integration'); run explicitly
    # with `pytest -m integration tests/framework/tools/terminal/…`.
    pytest.mark.integration,
]

_ENV_MARKER = "MODEX_TERMINAL_TEST_VAR"
_ENV_VALUE = "inherited-from-parent"


# ── helpers ───────────────────────────────────────────────────────


def _extract_output(xml_or_text: str) -> str:
    try:
        root = ET.fromstring(xml_or_text)
    except ET.ParseError:
        return xml_or_text
    return root.findtext("output", default=xml_or_text)


def _output_line(xml_or_text: str, marker: str) -> bool:
    text = _extract_output(xml_or_text)
    return any(line.strip() == marker for line in text.splitlines())


def _shell_platform() -> Platform:
    if sys.platform == "darwin":
        return Platform.DARWIN
    return Platform.LINUX


def _make_cfg() -> TerminalRuntimeConfig:
    return TerminalRuntimeConfig(
        command_deadline_seconds=15,
        prompt_stabilize_ms=200,
        input_wait_idle_ms=2_000,
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


async def _wait_for_status(
    manager: BaseTerminalManager,
    cfg: TerminalRuntimeConfig,
    targets: frozenset[TerminalCommandStatus],
    *,
    timeout: float = 10.0,
    interval: float = 0.2,
) -> TerminalCommandStatus:
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    last: TerminalCommandStatus = TerminalCommandStatus.UNKNOWN
    while loop.time() < deadline:
        session = await manager.get_default_session()
        if session is not None:
            last = await session.command_status(config=cfg)
        # When the session is gone (e.g. kill in tmux mode removes it),
        # ``last`` stays UNKNOWN — a terminal condition for target sets
        # that include UNKNOWN (i.e. no session == idle).
        if last in targets:
            return last
        await asyncio.sleep(interval)
    raise AssertionError(
        f"session did not reach {sorted(t.value for t in targets)} "
        f"within {timeout}s (last={last.value})"
    )


# ── bundle + parametrization ──────────────────────────────────────


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
    manager = create_terminal_manager(shell_info=shell_info, visibility=visibility, config=cfg)
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


def _visibility_params() -> list[pytest.param]:
    params: list[pytest.param] = []
    if _has_pexpect():
        params.append(pytest.param(TerminalVisibility.HIDDEN, id="hidden"))
    if _has_visible_support():
        params.append(pytest.param(TerminalVisibility.VISIBLE, id="visible"))
    return params


def _shell_params() -> list[pytest.param]:
    return [pytest.param(name, path, family, id=name) for name, path, family in _shells()]


# ── Test 1: terminal workflow — tabs + env + state + prompt + interrupt ─


@pytest.mark.parametrize("shell_name, shell_path, shell_family", _shell_params())
@pytest.mark.parametrize("visibility", _visibility_params())
@pytest.mark.timeout(150)
async def test_posix_terminal_workflow(
    visibility: TerminalVisibility,
    shell_name: str,
    shell_path: str,
    shell_family: ShellFamily,
) -> None:
    """Tab management, env inheritance, state persistence, interactive prompt, interrupt."""
    bundle = _make_bundle(visibility, shell_path, shell_family)
    b = bundle
    try:
        # 1. Open tab-a.
        result = await b.terminal.execute(action="open", name="tab-a")
        assert "Opened terminal tab 'tab-a'" in result, f"open tab-a failed: {result}"

        # 2. Env-var inheritance from parent process.
        result = await b.command.execute(command=f'echo "ENV=${{{_ENV_MARKER}}}"')
        assert _ENV_VALUE in _extract_output(result), f"env var not inherited:\n{result}"

        # 3. Open tab-b (auto-selected).
        result = await b.terminal.execute(action="open", name="tab-b")
        assert "Opened terminal tab 'tab-b'" in result, f"open tab-b failed: {result}"

        # 4. Command in tab-b.
        result = await b.command.execute(command='echo "TAB=tab-b"')
        assert _output_line(result, "TAB=tab-b"), f"command in tab-b failed:\n{result}"

        # 5. Switch back to tab-a.
        result = await b.terminal.execute(action="select", name="tab-a")
        assert "Selected 'tab-a'" in result, f"select tab-a failed: {result}"

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
                b.manager, b.cfg, frozenset({TerminalCommandStatus.WAITING_INPUT}), timeout=12.0
            )
        result = await b.process.execute(data="alice", submit=True)
        assert "got_alice" in result, f"process write did not answer:\n{result}"

        # 9. Interrupt + recovery — prompting command, ^C via process tool.
        result = await b.command.execute(
            command='printf "password: "; read v; sleep 30; echo DONE_NEVER'
        )
        if "waiting_input" not in result.lower():
            await _wait_for_status(
                b.manager, b.cfg, frozenset({TerminalCommandStatus.WAITING_INPUT}), timeout=12.0
            )
        await b.process.execute(data="^C")

        result = await b.command.execute(command='echo "RECOVERED"')
        assert _output_line(result, "RECOVERED"), f"recovery failed:\n{result}"

        # 10. Close both tabs.
        result = await b.terminal.execute(action="close", name="tab-a")
        assert "Closed" in result
        result = await b.terminal.execute(action="close", name="tab-b")
        assert "Closed" in result

        # 11. List should be empty.
        result = await b.terminal.execute(action="list")
        assert "No active terminal tabs" in result, f"list not empty:\n{result}"

        # 12. VISIBLE mode: the opened tab remains listed.
        if visibility == TerminalVisibility.VISIBLE:
            await b.terminal.execute(action="open", name="vis-check")
            await b.command.execute(command="echo visible_ok")
            result = await b.terminal.execute(action="list")
            assert "vis-check" in result, f"list should report vis-check:\n{result}"
            await b.terminal.execute(action="close", name="vis-check")
    finally:
        await _cleanup(bundle)


# ── Test 2: default recreation after manual close ─────────────────


@pytest.mark.parametrize("shell_name, shell_path, shell_family", _shell_params())
@pytest.mark.parametrize("visibility", _visibility_params())
@pytest.mark.timeout(120)
async def test_posix_command_recreate_default_after_manual_close(
    visibility: TerminalVisibility,
    shell_name: str,
    shell_path: str,
    shell_family: ShellFamily,
) -> None:
    """Manually killing the default terminal must not break the next command."""
    bundle = _make_bundle(visibility, shell_path, shell_family)
    b = bundle
    try:
        await b.terminal.execute(action="open", name="default")
        result = await b.command.execute(command='echo "before-close"')
        assert _output_line(result, "before-close"), f"initial command failed:\n{result}"

        session = await b.manager.get_default_session()
        assert session is not None
        await session.terminate()
        for _ in range(20):
            if not await session.is_alive():
                break
            await asyncio.sleep(0.1)
        assert not await session.is_alive(), "session did not die after terminate()"

        result = await b.command.execute(command='echo "after-close"')
        assert "New terminal tab" in result or _output_line(result, "after-close"), (
            f"expected recreate hint:\n{result}"
        )
        assert _output_line(result, "after-close"), f"recreated tab command failed:\n{result}"

        await b.terminal.execute(action="close", name="default")
    finally:
        await _cleanup(bundle)


# ── Test 3: full capability sample — every action in one pipeline ─


@pytest.mark.parametrize("shell_name, shell_path, shell_family", _shell_params())
@pytest.mark.parametrize("visibility", _visibility_params())
@pytest.mark.timeout(240)
async def test_posix_full_capability_sample(
    visibility: TerminalVisibility,
    shell_name: str,
    shell_path: str,
    shell_family: ShellFamily,
) -> None:
    """Sample tab management, command state, process writes, and interrupt recovery.

    Sequential tool contract (ADR-0044): one tool call at a time — bash returns
    a ``waiting_input`` advisory BEFORE the agent interacts via the process
    tool. No background command task runs while the session is read, so each
    PTY has a single reader at any moment — mirroring production ToolNode
    ordering, which executes tool calls strictly sequentially.
    """
    bundle = _make_bundle(visibility, shell_path, shell_family)
    b = bundle
    paste_file = "/tmp/modex_posix_capability_sample.txt"
    try:
        # ── 1. TerminalTool.open + CommandTool basics + env inheritance ──
        result = await b.terminal.execute(action="open", name="main")
        assert "Opened terminal tab 'main'" in result, f"open failed: {result}"

        result = await b.command.execute(command=f'echo "ENV=${{{_ENV_MARKER}}}"')
        assert _ENV_VALUE in _extract_output(result), f"env not inherited:\n{result}"

        result = await b.command.execute(command='echo "HELLO_9f1a"')
        assert _output_line(result, "HELLO_9f1a"), f"basic echo failed:\n{result}"

        # ── 2. TerminalTool.list ──
        result = await b.terminal.execute(action="list")
        assert "main" in result, f"list should show main:\n{result}"

        # ── 3. State persistence — cd + pwd ──
        await b.command.execute(command="cd /tmp")
        result = await b.command.execute(command="pwd")
        assert "/tmp" in _extract_output(result), f"cd not persisted:\n{result}"

        # ── 4. State persistence — export + echo ──
        await b.command.execute(command="export MY_VAR=hello456")
        result = await b.command.execute(command='echo "$MY_VAR"')
        assert "hello456" in _extract_output(result), f"export not persisted:\n{result}"

        # ── 5. ProcessTool.write — interactive prompt ──
        result = await b.command.execute(command='printf "name: "; read val; echo "got_$val"')
        if "waiting_input" not in result.lower():
            await _wait_for_status(
                b.manager, b.cfg, frozenset({TerminalCommandStatus.WAITING_INPUT}), timeout=12.0
            )
        result = await b.process.execute(data="alice", submit=True)
        assert "got_alice" in result, f"process.write did not answer:\n{result}"

        # ── 6. ProcessTool raw multiline write — prompting variant ──
        lines = ["paste-line-1", "paste-line-2", "paste-line-3"]
        result = await b.command.execute(command=f'printf "paste: "; head -n 3 > {paste_file}')
        if "waiting_input" not in result.lower():
            await _wait_for_status(
                b.manager, b.cfg, frozenset({TerminalCommandStatus.WAITING_INPUT}), timeout=12.0
            )
        result = await b.process.execute(data="\n".join(lines) + "\n", submit=False)

        result = await b.command.execute(command=f"cat {paste_file}")
        cat_text = _extract_output(result)
        for line in lines:
            assert line in cat_text, f"pasted line {line!r} missing:\n{result}"

        # ── 7. ProcessTool write with empty submitted input ──
        result = await b.command.execute(command='printf "confirm: "; read val; echo "result_$val"')
        if "waiting_input" not in result.lower():
            await _wait_for_status(
                b.manager, b.cfg, frozenset({TerminalCommandStatus.WAITING_INPUT}), timeout=12.0
            )
        result = await b.process.execute(data="", submit=True)
        assert "result_" in result, f"submitted empty write did not produce result_:\n{result}"

        # ── 8. ProcessTool ^C — interrupt a waiting read + recovery ──
        result = await b.command.execute(
            command='printf "password: "; read v; sleep 30; echo DONE_NEVER'
        )
        if "waiting_input" not in result.lower():
            await _wait_for_status(
                b.manager, b.cfg, frozenset({TerminalCommandStatus.WAITING_INPUT}), timeout=12.0
            )
        await b.process.execute(data="^C")

        result = await b.command.execute(command='echo "RECOVERED_7b2c"')
        assert _output_line(result, "RECOVERED_7b2c"), f"interrupt recovery failed:\n{result}"

        # ── 9. TerminalTool.select — multi-tab switching ──
        await b.terminal.execute(action="open", name="second")
        result = await b.command.execute(command='echo "SECOND_TAB"')
        assert _output_line(result, "SECOND_TAB"), f"command in second tab failed:\n{result}"

        result = await b.terminal.execute(action="select", name="main")
        assert "Selected 'main'" in result, f"select main failed: {result}"

        result = await b.command.execute(command='echo "BACK_MAIN"')
        assert _output_line(result, "BACK_MAIN"), f"command after select failed:\n{result}"

        # ── 10. TerminalTool.close + list empty ──
        for tab_name in list(b.manager._sessions):
            with contextlib.suppress(Exception):
                await b.terminal.execute(action="close", name=tab_name)

        result = await b.terminal.execute(action="list")
        assert "No active terminal tabs" in result, f"list not empty after close:\n{result}"
    finally:
        await _cleanup(bundle)
        with contextlib.suppress(Exception):
            os.remove(paste_file)
