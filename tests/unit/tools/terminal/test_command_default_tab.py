"""Regression test: CommandTool must reuse the default tab, not create a new one named after sessionId.

Bug: when _current_session_id is set (as NativeEnvInjectionHook does before every turn),
CommandTool.execute called get_or_create(sid) which created a new tab named after the
session ID, ignoring the default tab that terminal open created.
"""

from __future__ import annotations

import sys

import pytest

from modex_agent.runtime.env_context import _current_session_id
from modex_agent.tools.terminal.command_tool import CommandTool
from modex_agent.tools.terminal.config import TerminalRuntimeConfig
from modex_agent.tools.terminal.managers import create_terminal_manager
from modex_agent.tools.terminal.process_registry import ProcessRegistry
from modex_agent.tools.terminal.tool import TerminalTool
from modex_agent.tools.terminal.types import Platform, ShellFamily, ShellInfo, TerminalVisibility


def _platform_shell_info() -> ShellInfo:
    if sys.platform == "win32":
        return ShellInfo(
            family=ShellFamily.BASH, path=r"C:\Windows\System32\bash.exe", platform=Platform.WINDOWS
        )
    import shutil

    for name, family in [
        ("bash", ShellFamily.BASH),
        ("zsh", ShellFamily.ZSH),
        ("sh", ShellFamily.SH),
    ]:
        path = shutil.which(name)
        if path:
            plat = Platform.DARWIN if sys.platform == "darwin" else Platform.LINUX
            return ShellInfo(family=family, path=path, platform=plat)
    return ShellInfo(family=ShellFamily.SH, path="/bin/sh", platform=Platform.LINUX)


def _make_manager(visibility=TerminalVisibility.HIDDEN):
    si = _platform_shell_info()
    cfg = TerminalRuntimeConfig(command_deadline_seconds=5)
    mgr = create_terminal_manager(shell_info=si, visibility=visibility, config=cfg)
    reg = ProcessRegistry(config=cfg)
    return mgr, reg, cfg


@pytest.mark.asyncio
async def test_command_reuses_default_tab_when_session_id_set() -> None:
    """CommandTool must reuse the default tab, not create a new one named after sid."""
    mgr, reg, cfg = _make_manager()
    tt = TerminalTool(manager=mgr, registry=reg)
    ct = CommandTool(manager=mgr, registry=reg, config=cfg)

    await tt.execute(action="open", name="mytab")
    assert mgr.list_names() == ["mytab"]

    token = _current_session_id.set("conv123.main")
    try:
        await ct.execute(command="echo hello")
        names = mgr.list_names()
        assert "conv123.main" not in names, (
            f"CommandTool created a new tab named after sessionId. Expected ['mytab'], got {names}"
        )
        assert "mytab" in names, f"Default tab 'mytab' should still exist, got {names}"
    finally:
        _current_session_id.reset(token)
        for n in list(mgr.list_names()):
            await mgr.close(n)


@pytest.mark.asyncio
async def test_command_creates_default_when_no_tab_exists() -> None:
    """When no tab exists and no session_id is set, command creates 'default'."""
    mgr, reg, cfg = _make_manager()
    ct = CommandTool(manager=mgr, registry=reg, config=cfg)

    await ct.execute(command="echo hello")
    assert "default" in mgr.list_names()

    for n in list(mgr.list_names()):
        await mgr.close(n)


@pytest.mark.asyncio
async def test_command_creates_default_when_session_id_set_but_no_tab() -> None:
    """When session_id is set but no tab exists, command creates 'default', not sid-named."""
    mgr, reg, cfg = _make_manager()
    ct = CommandTool(manager=mgr, registry=reg, config=cfg)

    token = _current_session_id.set("conv456.main")
    try:
        await ct.execute(command="echo hello")
        names = mgr.list_names()
        assert "conv456.main" not in names, (
            f"CommandTool created a tab named after sessionId. Got {names}"
        )
        assert "default" in names, f"Should have a 'default' tab, got {names}"
    finally:
        _current_session_id.reset(token)
        for n in list(mgr.list_names()):
            await mgr.close(n)
