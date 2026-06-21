"""Default-cwd binding for terminal sessions (workspace multi-live).

A terminal-enabled pool must open its default session in the WORKSPACE's
target directory, not the process CWD. The manager carries a ``default_cwd``
(here: the workspace target); ``get_or_create`` falls back to it when no
explicit ``cwd`` is passed. An explicit ``cwd`` always wins.
"""

from __future__ import annotations

import pytest

from framework.tools.terminal.managers import BaseTerminalManager
from framework.tools.terminal.types import (
    Platform,
    ShellFamily,
    ShellInfo,
    TerminalVisibility,
)


class _FakeBackend:
    """Minimal backend: TerminalSession construction never starts it."""

    visibility = TerminalVisibility.HIDDEN
    window_title = "fake"


def _make_manager(default_cwd: str | None) -> BaseTerminalManager:
    return BaseTerminalManager(
        shell_info=ShellInfo(ShellFamily.BASH, "bash", Platform.LINUX),
        visibility=TerminalVisibility.HIDDEN,
        backend_factory=_FakeBackend,
        default_cwd=default_cwd,
    )


@pytest.mark.asyncio
async def test_default_session_uses_default_cwd(tmp_path):
    manager = _make_manager(default_cwd=str(tmp_path))
    session = await manager.get_default()
    assert session.cwd == str(tmp_path)


@pytest.mark.asyncio
async def test_explicit_cwd_wins_over_default(tmp_path):
    manager = _make_manager(default_cwd=str(tmp_path))
    session = await manager.get_or_create("explicit", cwd="/elsewhere")
    assert session.cwd == "/elsewhere"


@pytest.mark.asyncio
async def test_no_default_cwd_yields_none(tmp_path):
    manager = _make_manager(default_cwd=None)
    session = await manager.get_or_create("x")
    assert session.cwd is None
