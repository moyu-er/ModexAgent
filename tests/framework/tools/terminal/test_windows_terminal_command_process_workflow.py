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
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from modex_agent.tools.terminal.command_tool import CommandTool
from modex_agent.tools.terminal.config import TerminalRuntimeConfig
from modex_agent.tools.terminal.managers import create_terminal_manager
from modex_agent.tools.terminal.process_tool import ProcessTool
from modex_agent.tools.terminal.process_registry import ProcessRegistry
from modex_agent.tools.terminal.tool import TerminalTool
from modex_agent.tools.terminal.types import Platform, ShellFamily, ShellInfo, TerminalVisibility

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only real PTY workflow")


# Environment marker used to prove terminals inherit the parent env.
_ENV_MARKER = "MODEX_TERMINAL_TEST_VAR"
_ENV_VALUE = "inherited-from-parent"


def _wsl_bash() -> str | None:
    """WSL bash is preferred on Windows when available."""
    wsl = r"C:\Windows\System32\bash.exe"
    return wsl if Path(wsl).is_file() else None


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
        pytest.param(TerminalVisibility.VISIBLE, id="visible"),
    ],
)
@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_terminal_command_process_workflow(
    visibility: TerminalVisibility,
    shell_name: str,
    shell_finder: callable,
) -> None:
    """Full tab-switching, command, and process interaction on a real Windows PTY."""
    shell_path = shell_finder()
    if shell_path is None:
        pytest.skip(f"{shell_name} bash not available on this Windows machine")

    terminal_tool, command_tool, process_tool = _make_tools(visibility, shell_path)

    # 1. Open tab-a.
    result = await terminal_tool.execute(action="open", name="tab-a")
    assert "Opened terminal 'tab-a'" in result

    # 2. Run a command in tab-a and prove env inheritance from the parent process.
    result = await command_tool.execute(command=f'echo "ENV=${{{_ENV_MARKER}}}"')
    output_text = _extract_output(result)
    assert f"ENV={_ENV_VALUE}" in output_text, (
        f"Expected env var expansion in output, got: {output_text!r}\nFull result: {result!r}"
    )

    # 3. Open tab-b; it becomes the new default automatically.
    result = await terminal_tool.execute(action="open", name="tab-b")
    assert "Opened terminal 'tab-b'" in result

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

    result = await process_tool.execute(action="write", data="hello", submit=True)
    assert "got hello" in result, f"Process write did not produce expected output: {result}"

    # 8. Close both tabs.
    result = await terminal_tool.execute(action="close", name="tab-a")
    assert "Closed terminal 'tab-a'" in result

    result = await terminal_tool.execute(action="close", name="tab-b")
    assert "Closed terminal 'tab-b'" in result

    # The list should now be empty.
    result = await terminal_tool.execute(action="list")
    assert "No active terminals" in result


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
        pytest.param(TerminalVisibility.VISIBLE, id="visible"),
    ],
)
@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_command_recreate_default_after_manual_close(
    visibility: TerminalVisibility,
    shell_name: str,
    shell_finder: callable,
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
    assert "Opened terminal 'default'" in result

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
