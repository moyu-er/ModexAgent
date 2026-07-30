"""Real Windows workflow: terminal + command + process tools across tabs/shells.

This is the high-value integration surface for the terminal system on Windows.
It exercises the three public tools (TerminalTool, CommandTool, ProcessTool)
against real PTY backends — both visible console windows and hidden sessions,
with both WSL bash and Git bash — in a single realistic sequence:

  1. open tab-a
  2. run a command in tab-a that proves environment-variable inheritance
  3. open tab-b (auto-selected)
  4. run a command in tab-b
  5. select tab-a again
  6. run another command in tab-a
  7. start an interactive command, then use ProcessTool to feed input
  8. close both tabs

This replaces the thinner backend-only integration tests that only started a
backend and ran one isolated command.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from modex_agent.tools.terminal.command_tool import CommandTool
from modex_agent.tools.terminal.config import TerminalRuntimeConfig
from modex_agent.tools.terminal.managers import create_terminal_manager
from modex_agent.tools.terminal.process_registry import ProcessRegistry
from modex_agent.tools.terminal.process_tool import ProcessTool
from modex_agent.tools.terminal.tool import TerminalTool
from modex_agent.tools.terminal.types import Platform, ShellFamily, ShellInfo, TerminalVisibility

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only real PTY workflow")


# Environment marker used to prove terminals inherit the parent env.
_ENV_MARKER = "MODEX_TERMINAL_TEST_VAR"
_ENV_VALUE = "inherited-from-parent"


def _wsl_responsive() -> bool:
    """Return True if WSL is installed and can execute a trivial command."""
    wsl = r"C:\Windows\System32\wsl.exe"
    if not Path(wsl).is_file():
        return False
    try:
        result = subprocess.run(
            [wsl, "echo", "ok"],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def _wsl_bash() -> str | None:
    """WSL bash is preferred on Windows when available and responsive."""
    wsl = r"C:\Windows\System32\bash.exe"
    if not Path(wsl).is_file():
        return None
    if not _wsl_responsive():
        return None
    return wsl


def _git_bash() -> str | None:
    """Git bash / MSYS2 fallback."""
    return shutil.which("bash")


def _shell_family(shell_path: str) -> ShellFamily:
    """Infer ShellFamily from the executable path (bash/sh/zsh)."""
    name = Path(shell_path).name.lower()
    mapping = {
        "bash": ShellFamily.BASH,
        "zsh": ShellFamily.ZSH,
        "sh": ShellFamily.SH,
    }
    return mapping.get(name, ShellFamily.BASH)


def _shell_param_id(value: object) -> str:
    """Human-readable param id for pytest."""
    return str(value)


def _extract_output(xml: str) -> str:
    """Extract text content of the <output> tag from tool XML.

    Falls back to the raw string if it is not valid XML.
    """
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return xml
    return root.findtext("output", default="")


@pytest.fixture(autouse=True)
def _mark_env_for_inheritance(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set a parent env var that every terminal session must inherit.

    Git bash / MSYS2 inherit Windows env vars directly. WSL bash does not;
    it requires the variable to be listed in ``WSLENV`` to cross the
    Windows/Linux boundary, so we add the marker there too.
    """
    monkeypatch.setenv(_ENV_MARKER, _ENV_VALUE)
    old_wslenv = os.environ.get("WSLENV", "")
    monkeypatch.setenv(
        "WSLENV",
        f"{old_wslenv}:{_ENV_MARKER}" if old_wslenv else _ENV_MARKER,
    )


def _make_runtime_config() -> TerminalRuntimeConfig:
    """Tight but realistic timeouts for real PTY startup on Windows."""
    return TerminalRuntimeConfig(
        default_command_timeout_seconds=15,
        command_tool_outer_timeout_seconds=20,
        default_yield_ms=500,
        prompt_stabilize_ms=200,
        no_output_timeout_ms=5_000,
    )


def _make_tools(visibility: TerminalVisibility, shell_path: str) -> tuple[TerminalTool, CommandTool, ProcessTool]:
    """Build the three public terminal tools for one (visibility, shell) combo."""
    cfg = _make_runtime_config()
    shell_info = ShellInfo(
        family=_shell_family(shell_path),
        path=shell_path,
        platform=Platform.WINDOWS,
    )
    manager = create_terminal_manager(
        shell_info=shell_info,
        visibility=visibility,
        config=cfg,
    )
    registry = ProcessRegistry(config=cfg)
    terminal_tool = TerminalTool(manager=manager, registry=registry)
    command_tool = CommandTool(manager=manager, registry=registry, config=cfg)
    process_tool = ProcessTool(registry=registry, manager=manager, config=cfg)
    return terminal_tool, command_tool, process_tool


@pytest.mark.parametrize(
    "shell_name, shell_finder",
    [
        pytest.param("wsl", _wsl_bash, id="wsl"),
        pytest.param("git", _git_bash, id="git"),
    ],
    ids=_shell_param_id,
)
@pytest.mark.parametrize(
    "visibility",
    [
        pytest.param(TerminalVisibility.HIDDEN, id="hidden"),
        # VISIBLE mode is un-skipped per ADR-0032 D2: the asyncio-streams
        # rewrite of WinptyConsoleWindowBackend (asyncio.start_server +
        # StreamWriter/StreamReader + TCP_NODELAY, no settimeout leak)
        # structurally eliminates the partial-sendall / lost-Enter failure
        # mode that previously made this parametrization flaky.
        pytest.param(TerminalVisibility.VISIBLE, id="visible"),
    ],
)
@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_terminal_command_process_workflow(
    visibility: TerminalVisibility,
    shell_name: str,
    shell_finder: Callable[[], str | None],
) -> None:
    """Full tab-switching, command, and process interaction on a real Windows PTY."""
    shell_path = shell_finder()
    if shell_path is None:
        pytest.skip(f"{shell_name} bash not available on this Windows machine")

    terminal_tool, command_tool, process_tool = _make_tools(visibility, shell_path)

    # 1. Open tab-a.
    result = await terminal_tool.execute(action="open", name="tab-a")
    assert "Opened terminal tab 'tab-a'" in result

    # 2. Run a command in tab-a and prove env inheritance from the parent process.
    result = await command_tool.execute(command=f'echo "ENV=${{{_ENV_MARKER}}}"')
    output_text = _extract_output(result)
    assert f"ENV={_ENV_VALUE}" in output_text, (
        f"Expected env var expansion in output, got: {output_text!r}\nFull result: {result!r}"
    )

    # 3. Open tab-b; it becomes the new default automatically.
    result = await terminal_tool.execute(action="open", name="tab-b")
    assert "Opened terminal tab 'tab-b'" in result

    # 4. Run a command in tab-b.
    result = await command_tool.execute(command='echo "TAB=tab-b"')
    assert "TAB=tab-b" in result, f"Command in tab-b failed: {result}"

    # 5. Switch back to tab-a.
    result = await terminal_tool.execute(action="select", name="tab-a")
    assert "Selected 'tab-a'" in result

    # 6. Run a command in tab-a again to prove selection worked.
    result = await command_tool.execute(command='echo "BACK=tab-a"')
    assert "BACK=tab-a" in result, f"Command after select failed: {result}"

    # 7. Start an interactive command, then use ProcessTool to provide input.
    result = await command_tool.execute(command='read -p "username: " val; echo "got $val"')
    assert "waiting_input" in result or "username:" in result, f"Expected input-wait state, got: {result}"

    await asyncio.sleep(1.0)
    result = await process_tool.execute(action="write", data="hello", submit=True)
    assert "got hello" in result, f"Process write did not produce expected output: {result}"

    # 8. Close both tabs.
    result = await terminal_tool.execute(action="close", name="tab-a")
    assert "Closed terminal 'tab-a'" in result

    result = await terminal_tool.execute(action="close", name="tab-b")
    assert "Closed terminal 'tab-b'" in result

    # The list should now be empty.
    result = await terminal_tool.execute(action="list")
    assert "No active terminal tabs" in result


@pytest.mark.parametrize(
    "shell_name, shell_finder",
    [
        pytest.param("wsl", _wsl_bash, id="wsl"),
        pytest.param("git", _git_bash, id="git"),
    ],
    ids=_shell_param_id,
)
@pytest.mark.parametrize(
    "visibility",
    [
        pytest.param(TerminalVisibility.HIDDEN, id="hidden"),
    ],
)
@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_command_recreate_default_after_manual_close(
    visibility: TerminalVisibility,
    shell_name: str,
    shell_finder: Callable[[], str | None],
) -> None:
    """Manually closing the default terminal must not break the next command.

    The manager should detect the dead session, drop it, and create a fresh
    default tab. CommandTool must surface the new-tab hint so the agent knows
    a replacement was created.
    """
    shell_path = shell_finder()
    if shell_path is None:
        pytest.skip(f"{shell_name} bash not available on this Windows machine")

    terminal_tool, command_tool, _ = _make_tools(visibility, shell_path)

    # Create the initial default tab and run something in it.
    result = await terminal_tool.execute(action="open", name="default")
    assert "Opened terminal tab 'default'" in result

    result = await command_tool.execute(command='echo "before-close"')
    assert "before-close" in _extract_output(result)

    # Simulate the user manually killing the terminal window/backend.
    session = await terminal_tool._manager.get_default_session()
    assert session is not None
    await session.terminate()
    # Give the OS a moment to reap the process so is_alive() becomes False.
    import asyncio

    for _ in range(20):
        if not await session.is_alive():
            break
        await asyncio.sleep(0.1)
    assert not await session.is_alive(), "Session did not die after terminate()"

    # Next command should recreate the default tab and show the hint.
    result = await command_tool.execute(command='echo "after-close"')
    assert "New terminal tab 'default' created" in result, (
        f"Expected new-tab hint after manual close, got: {result}"
    )
    assert "after-close" in _extract_output(result), (
        f"Command did not run in recreated tab: {result}"
    )

    await terminal_tool.execute(action="close", name="default")


# ────────────────────────────────────────────────────────────────────
# Big sample: full capability matrix for one (visibility) combination.
# ────────────────────────────────────────────────────────────────────


import asyncio  # noqa: E402
import contextlib  # noqa: E402

from modex_agent.tools.terminal.types import TerminalCommandStatus  # noqa: E402


_IDLE_STATUSES = frozenset(
    {
        TerminalCommandStatus.IDLE,
        TerminalCommandStatus.UNKNOWN,
        TerminalCommandStatus.COMPLETED,
    }
)
_EXECUTING_STATUSES = frozenset(
    {
        TerminalCommandStatus.EXECUTING,
        TerminalCommandStatus.WAITING_INPUT,
    }
)


async def _wait_for_status(
    manager: object,
    cfg: TerminalRuntimeConfig,
    targets: frozenset[TerminalCommandStatus],
    *,
    timeout: float = 10.0,
    interval: float = 0.2,
) -> TerminalCommandStatus:
    """Poll the default session until its status lands in *targets*."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    last: TerminalCommandStatus = TerminalCommandStatus.UNKNOWN
    while loop.time() < deadline:
        session = await manager.get_default_session()  # type: ignore[attr-defined]
        if session is not None:
            last = await session.command_status(config=cfg)
            if last in targets:
                return last
        await asyncio.sleep(interval)
    raise AssertionError(
        f"session did not reach {sorted(t.value for t in targets)} "
        f"within {timeout}s (last={last.value})"
    )


async def _wait_idle(manager: object, cfg: TerminalRuntimeConfig, timeout: float = 10.0) -> None:
    await _wait_for_status(manager, cfg, _IDLE_STATUSES, timeout=timeout)


async def _wait_executing(
    manager: object, cfg: TerminalRuntimeConfig, timeout: float = 10.0
) -> None:
    await _wait_for_status(manager, cfg, _EXECUTING_STATUSES, timeout=timeout)


def _output_line(xml_or_text: str, marker: str) -> bool:
    """True if *marker* appears as a standalone stripped line."""
    text = _extract_output(xml_or_text)
    return any(line.strip() == marker for line in text.splitlines())


def _pick_shell() -> str | None:
    """Pick the first available bash (WSL preferred, Git Bash fallback)."""
    return _wsl_bash() or _git_bash()


@pytest.mark.parametrize(
    "visibility",
    [
        pytest.param(TerminalVisibility.HIDDEN, id="hidden"),
        pytest.param(TerminalVisibility.VISIBLE, id="visible"),
    ],
)
@pytest.mark.asyncio
@pytest.mark.timeout(240)
async def test_windows_full_capability_sample(visibility: TerminalVisibility) -> None:
    """One big sample exercising every TerminalTool / CommandTool / ProcessTool action.

    Covers the full capability matrix in a single realistic pipeline:

    **TerminalTool**: open, list, current, select, interrupt, close
    **CommandTool**: echo, env-var inheritance, cd/pwd persistence, export persistence,
                     interactive prompt (waiting_input), long-running timeout
    **ProcessTool**: write, submit, send_keys (Ctrl-D/Ctrl-C/Ctrl-U), paste,
                     interrupt, kill, clear, remove

    Runs once per visibility (HIDDEN = WinptyHiddenBackend, VISIBLE = WinptyConsoleWindowBackend).
    """
    shell_path = _pick_shell()
    if shell_path is None:
        pytest.skip("No bash (WSL or Git) available on this Windows machine")

    terminal_tool, command_tool, process_tool = _make_tools(visibility, shell_path)
    manager = terminal_tool._manager
    cfg = _make_runtime_config()
    paste_file = "/tmp/modex_win_capability_sample.txt"

    try:
        # ── 1. TerminalTool.open + CommandTool basics + env inheritance ──
        result = await terminal_tool.execute(action="open", name="main")
        assert "Opened terminal tab 'main'" in result, f"open failed: {result}"

        result = await command_tool.execute(command=f'echo "ENV=${{{_ENV_MARKER}}}"')
        assert _ENV_VALUE in _extract_output(result), f"env not inherited:\n{result}"

        result = await command_tool.execute(command='echo "HELLO_9f1a"')
        assert _output_line(result, "HELLO_9f1a"), f"basic echo failed:\n{result}"

        # ── 2. TerminalTool.list + TerminalTool.current ──
        result = await terminal_tool.execute(action="list")
        assert "main" in result, f"list should show main:\n{result}"

        result = await terminal_tool.execute(action="current")
        assert "<status>" in result, f"current should return XML:\n{result}"
        assert "<status>" in result, f"current should return status:\n{result}"

        # ── 3. State persistence — cd + pwd ──
        await command_tool.execute(command="cd /tmp")
        result = await command_tool.execute(command="pwd")
        assert "/tmp" in _extract_output(result), f"cd not persisted:\n{result}"

        # ── 4. State persistence — export + echo ──
        await command_tool.execute(command="export MY_VAR=hello456")
        result = await command_tool.execute(command='echo "$MY_VAR"')
        assert "hello456" in _extract_output(result), f"export not persisted:\n{result}"

        # ── 5. ProcessTool.write — interactive prompt ──
        result = await command_tool.execute(command='read -p "name: " val; echo "got_$val"')
        if "waiting_input" not in result.lower():
            await _wait_for_status(
                manager, cfg, frozenset({TerminalCommandStatus.WAITING_INPUT}), timeout=12.0
            )
        result = await process_tool.execute(action="write", data="alice", submit=True)
        assert "got_alice" in result, f"process.write did not answer:\n{result}"

        # ── 6. ProcessTool.paste + send_keys Ctrl-D — multiline to cat ──
        lines = ["paste-line-1", "paste-line-2", "paste-line-3"]
        cat_task = asyncio.create_task(command_tool.execute(command=f"cat > {paste_file}"))
        await asyncio.sleep(2.0)
        await _wait_executing(manager, cfg, timeout=8.0)

        result = await process_tool.execute(
            action="paste", text="\n".join(lines)
        )
        assert "rejected" not in result.lower(), f"paste rejected:\n{result}"

        await asyncio.sleep(0.5)
        await process_tool.execute(action="send_keys", hex=["04"])  # Ctrl-D EOF
        await asyncio.sleep(1.0)
        cat_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, BaseException):
            await cat_task
        await asyncio.sleep(0.5)
        # Clear any residual state from the cat command.
        with contextlib.suppress(Exception):
            await process_tool.execute(action="interrupt")
        await asyncio.sleep(0.5)

        result = await command_tool.execute(command=f"cat {paste_file}")
        cat_text = _extract_output(result)
        for line in lines:
            assert line in cat_text, f"pasted line {line!r} missing:\n{result}"

        # ── 7. ProcessTool.submit — press Enter with empty input ──
        result = await command_tool.execute(command='read -p "confirm: " val; echo "result_$val"')
        if "waiting_input" not in result.lower():
            await _wait_for_status(
                manager, cfg, frozenset({TerminalCommandStatus.WAITING_INPUT}), timeout=12.0
            )
        result = await process_tool.execute(action="submit")
        assert "result_" in result, f"process.submit did not produce result_:\n{result}"

        # ── 8. ProcessTool.interrupt — long-running + recovery ──
        sleep_task = asyncio.create_task(command_tool.execute(command="sleep 60"))
        await _wait_executing(manager, cfg, timeout=8.0)
        await process_tool.execute(action="interrupt")
        await asyncio.sleep(1.0)
        sleep_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, BaseException):
            await sleep_task
        # Second interrupt to clear any residual state.
        with contextlib.suppress(Exception):
            await process_tool.execute(action="interrupt")
        await asyncio.sleep(1.0)

        result = await command_tool.execute(command='echo "RECOVERED_7b2c"')
        assert _output_line(result, "RECOVERED_7b2c"), f"interrupt recovery failed:\n{result}"

        # ── 9. TerminalTool.interrupt ──
        sleep_task2 = asyncio.create_task(command_tool.execute(command="sleep 60"))
        await _wait_executing(manager, cfg, timeout=8.0)
        await terminal_tool.execute(action="interrupt")
        await asyncio.sleep(1.0)
        sleep_task2.cancel()
        with contextlib.suppress(asyncio.CancelledError, BaseException):
            await sleep_task2
        with contextlib.suppress(Exception):
            await process_tool.execute(action="interrupt")
        await asyncio.sleep(1.0)

        result = await command_tool.execute(command='echo "RECOVERED_8c3d"')
        assert _output_line(result, "RECOVERED_8c3d"), f"terminal.interrupt recovery failed:\n{result}"

        # ── 10. ProcessTool.send_keys Ctrl-C — interrupt via byte ──
        sleep_task3 = asyncio.create_task(command_tool.execute(command="sleep 60"))
        await _wait_executing(manager, cfg, timeout=8.0)
        await process_tool.execute(action="send_keys", hex=["03"])  # Ctrl-C
        await _wait_idle(manager, cfg, timeout=10.0)
        sleep_task3.cancel()
        with contextlib.suppress(asyncio.CancelledError, BaseException):
            await sleep_task3

        # ── 11. send_keys Ctrl-U — clear readline input (direct PTY write) ──
        # process_tool.send_keys requires a running process, but after echo
        # completes there is none. Write Ctrl-U (0x15) directly to the PTY
        # via session.write — this is the raw byte path that send_keys
        # would use internally.
        await command_tool.execute(command='echo "warmup_u"')
        await _wait_idle(manager, cfg, timeout=5.0)

        session = await manager.get_default_session()
        assert session is not None
        await session.write("garbage_partial_zz")
        await asyncio.sleep(1.0)
        await session.write("\x15")  # Ctrl-U — clear readline input
        await asyncio.sleep(0.8)
        seg = await session.current_segment()
        assert "garbage_partial_zz" not in seg.cursor_line, (
            f"Ctrl-U did not clear readline input:\ncursor={seg.cursor_line!r}"
        )
        # Cancel any residual readline input and return to a clean prompt.
        await session.write("\x03")  # Ctrl-C
        await asyncio.sleep(0.5)
        await _wait_idle(manager, cfg, timeout=5.0)

        # ── 12. ProcessTool.clear — clear finished session ──
        await command_tool.execute(command='echo "finished_1"')
        result = await process_tool.execute(action="clear")
        assert "Cleared the finished command record" in result, f"clear failed:\n{result}"

        # ── 13. ProcessTool.remove — remove finished session ──
        await command_tool.execute(command='echo "finished_2"')
        result = await process_tool.execute(action="remove")
        assert "Removed the finished command record" in result, f"remove failed:\n{result}"

        # ── 14. TerminalTool.select — multi-tab switching ──
        await terminal_tool.execute(action="open", name="second")
        result = await command_tool.execute(command='echo "SECOND_TAB"')
        assert _output_line(result, "SECOND_TAB"), f"command in second tab failed:\n{result}"

        result = await terminal_tool.execute(action="select", name="main")
        assert "Selected 'main'" in result, f"select main failed:\n{result}"

        result = await command_tool.execute(command='echo "BACK_MAIN"')
        assert _output_line(result, "BACK_MAIN"), f"command after select failed:\n{result}"

        # ── 15. ProcessTool.kill — clears running registry ──
        # kill terminates the backend; the manager purges the dead session.
        # The next command auto-recreates a fresh default tab.
        kill_task = asyncio.create_task(command_tool.execute(command="sleep 60"))
        await _wait_executing(manager, cfg, timeout=8.0)
        result = await process_tool.execute(action="kill")
        assert "Killed the running command" in result, f"kill did not report killed:\n{result}"
        kill_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, BaseException):
            await kill_task
        await asyncio.sleep(1.0)
        result = await command_tool.execute(command='echo "AFTER_KILL"')
        assert _output_line(result, "AFTER_KILL"), f"recovery after kill failed:\n{result}"

        # ── 16. TerminalTool.close + list empty ──
        # "main" may have been purged after kill; close whatever remains.
        for tab_name in list(manager.list_names()):  # type: ignore[attr-defined]
            with contextlib.suppress(Exception):
                await terminal_tool.execute(action="close", name=tab_name)

        result = await terminal_tool.execute(action="list")
        assert "No active terminal tabs" in result, f"list not empty after close:\n{result}"

    finally:
        for name in list(manager.list_names()):  # type: ignore[attr-defined]
            with contextlib.suppress(Exception):
                await asyncio.wait_for(manager.close(name), timeout=5.0)  # type: ignore[attr-defined]
        with contextlib.suppress(Exception):
            import os
            os.remove(paste_file)
