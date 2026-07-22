"""Shared fixtures for the Windows terminal pytest suite.

A single ``tools_bundle`` fixture yields a fresh (TerminalTool, CommandTool,
ProcessTool, registry) bound to one (visibility) combination, with tight
timeouts (≤8s per command, ≤25s per test) and guaranteed cleanup.

Every test in this directory is skipped on non-Windows platforms.
"""
from __future__ import annotations

import asyncio
import shutil
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import NamedTuple

import pytest

from modex_agent.tools.terminal.command_tool import CommandTool
from modex_agent.tools.terminal.config import TerminalRuntimeConfig
from modex_agent.tools.terminal.managers import create_terminal_manager
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

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only")


_COMMAND_TIMEOUT_S = 15.0
_PROCESS_TIMEOUT_S = 20.0


def _git_bash() -> str | None:
    """Locate Git Bash via the git install path (resilient to PATH ordering)."""
    git = shutil.which("git")
    if not git:
        return None
    git_dir = Path(git).resolve().parent
    for cand in [git_dir / "bash.exe", git_dir.parent / "bin" / "bash.exe",
                 git_dir.parent / "usr" / "bin" / "bash.exe"]:
        if cand.is_file():
            return str(cand)
    return None


def _wsl_bash() -> str | None:
    """WSL bash if the WSL echo round-trips successfully."""
    import subprocess
    bash = r"C:\Windows\System32\bash.exe"
    if not Path(bash).is_file():
        return None
    try:
        r = subprocess.run(
            [bash, "-c", "echo ok"],
            capture_output=True,
            text=True,
            timeout=8,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    return bash if r.returncode == 0 and "ok" in r.stdout else None


def _pick_shell() -> str | None:
    """Pick a shell for tests. Prefers WSL bash (matches production default),
    falls back to Git Bash (always available when git is installed).
    """
    return _wsl_bash() or _git_bash()


def _shell_family(path: str) -> ShellFamily:
    name = Path(path).name.lower()
    return {
        "bash": ShellFamily.BASH,
        "zsh": ShellFamily.ZSH,
        "sh": ShellFamily.SH,
    }.get(name, ShellFamily.BASH)


class ToolsBundle(NamedTuple):
    terminal: TerminalTool
    command: CommandTool
    process: ProcessTool
    registry: ProcessRegistry
    shell_path: str
    cfg: TerminalRuntimeConfig


@pytest.fixture
def visibility(request: pytest.FixtureRequest) -> TerminalVisibility:
    """Visibility parametrized via ``indirect=True`` from each test module.

    Tests that don't parametrize get HIDDEN by default.
    """
    return getattr(request, "param", TerminalVisibility.HIDDEN)


@pytest.fixture
async def cfg() -> AsyncIterator[TerminalRuntimeConfig]:
    cfg_obj = TerminalRuntimeConfig(
        default_command_timeout_seconds=int(_COMMAND_TIMEOUT_S),
        command_tool_outer_timeout_seconds=int(_COMMAND_TIMEOUT_S) + 4,
        default_yield_ms=300,
        prompt_stabilize_ms=200,
        no_output_timeout_ms=8_000,
        # Idle-based input-wait detection threshold. Tests use a long value
        # so a freshly-started ``sleep 60`` (no output yet) is not
        # misclassified as waiting for input. ``read -s`` is detected by the
        # content-based marker path (INPUT_PROMPT_MARKERS) and by poll_loop's
        # idle fallback inside CommandTool (which has its own threshold).
        input_wait_idle_ms=6_000,
    )
    yield cfg_obj


@pytest.fixture
async def tools(visibility: TerminalVisibility, cfg: TerminalRuntimeConfig) -> AsyncIterator[ToolsBundle]:
    """Build the three tools for the requested visibility. Closes all tabs on teardown."""
    shell_path = _pick_shell()
    if shell_path is None:
        pytest.skip("No bash available on this Windows machine")

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

    # Warm up the backend: VISIBLE+WSL takes ~13s to spin up. Running an
    # initial echo here means the first real test command sees a stable
    # prompt instead of racing with PTY startup banner drain.
    warm = await asyncio.wait_for(
        command_tool.execute(command="echo warmup_a3f1"),
        timeout=25.0,
    )
    assert output_of(warm, "warmup_a3f1"), f"warmup did not complete:\n{warm}"

    try:
        yield ToolsBundle(
            terminal=terminal_tool,
            command=command_tool,
            process=process_tool,
            registry=registry,
            shell_path=shell_path,
            cfg=cfg,
        )
    finally:
        for name in manager.list_names():
            try:
                await asyncio.wait_for(manager.close(name), timeout=3.0)
            except TimeoutError:
                pass


async def run_command(bundle: ToolsBundle, command: str, timeout: float = _PROCESS_TIMEOUT_S) -> str:
    """Run a command via CommandTool with a hard outer timeout."""
    return await asyncio.wait_for(bundle.command.execute(command=command), timeout=timeout)


async def wait_for_idle(tools: ToolsBundle, timeout: float = 5.0) -> None:
    """Block until the default session reaches IDLE/UNKNOWN/COMPLETED.

    Use after a warmup command to ensure the prompt is fully stable before
    sending bytes directly to the PTY (e.g. before ``send_keys`` tests).
    """
    await wait_for_status(
        tools,
        frozenset({TerminalCommandStatus.IDLE, TerminalCommandStatus.UNKNOWN, TerminalCommandStatus.COMPLETED}),
        timeout=timeout,
    )


async def session_status(tools: ToolsBundle) -> TerminalCommandStatus:
    """Read the live status of the default session (used by long-running flows)."""
    session = await tools.terminal._manager.get_default()
    assert session is not None
    return await session.command_status(config=tools.cfg)


async def wait_for_status(
    tools: ToolsBundle,
    target: TerminalCommandStatus | frozenset[TerminalCommandStatus],
    *,
    timeout: float = 8.0,
    interval: float = 0.2,
) -> TerminalCommandStatus:
    """Poll until the default session reaches ``target`` (or a set of targets)."""
    targets = target if isinstance(target, frozenset) else frozenset([target])
    deadline = asyncio.get_event_loop().time() + timeout
    last: TerminalCommandStatus = TerminalCommandStatus.UNKNOWN
    while asyncio.get_event_loop().time() < deadline:
        last = await session_status(tools)
        if last in targets:
            return last
        await asyncio.sleep(interval)
    raise AssertionError(
        f"session did not reach {sorted(t.value for t in targets)} within {timeout}s (last={last.value})"
    )


async def current_text(tools: ToolsBundle) -> str:
    """Invoke ``terminal current`` and return its raw output (incl. cursor/output XML)."""
    return await tools.terminal.execute(action="current")


async def drain_current(tools: ToolsBundle, needle: str, *, attempts: int = 30, delay: float = 0.3) -> str:
    """Repeatedly ``terminal current`` until ``needle`` appears (case-sensitive)."""
    last = ""
    for _ in range(attempts):
        last = await current_text(tools)
        if needle in last:
            return last
        await asyncio.sleep(delay)
    return last


def output_of(xml_or_text: str, marker: str) -> bool:
    """True if ``marker`` appears on its own line — distinguishes ``echo`` stdout
    from a readline input echo that includes the command alongside the prompt.
    """
    return any(line.strip() == marker for line in xml_or_text.splitlines())
