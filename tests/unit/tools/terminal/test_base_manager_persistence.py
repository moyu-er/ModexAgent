"""JSON persistence — flag-guarded via storage_dir=None (i.e. no persistence)."""

from __future__ import annotations

from pathlib import Path

import pytest

from modex_agent.tools.terminal.managers import BaseTerminalManager
from modex_agent.tools.terminal.types import Platform, ShellFamily, ShellInfo, TerminalVisibility

_TEST_SHELL_INFO = ShellInfo(family=ShellFamily.BASH, path="/bin/bash", platform=Platform.LINUX)


class _FakeBackend:
    visibility = TerminalVisibility.HIDDEN


@pytest.mark.asyncio
async def test_lean_form_does_not_save_or_load(tmp_path: Path) -> None:
    mgr = BaseTerminalManager(
        shell_info=_TEST_SHELL_INFO,
        visibility=TerminalVisibility.HIDDEN,
        backend_factory=_FakeBackend,
    )
    await mgr.save_state()
    await mgr.load_state()
    assert mgr.list_names() == []


@pytest.mark.asyncio
async def test_save_load_round_trip_with_storage_dir(tmp_path: Path) -> None:
    mgr = BaseTerminalManager(
        shell_info=_TEST_SHELL_INFO,
        visibility=TerminalVisibility.HIDDEN,
        backend_factory=_FakeBackend,
        storage_dir=tmp_path,
    )
    session = await mgr.get_or_create(name="alpha")
    from modex_agent.tools.terminal.session import CommandRecord

    session._history.append(CommandRecord(command="echo hi", output="hi\n"))
    await mgr.save_state()

    mgr2 = BaseTerminalManager(
        shell_info=_TEST_SHELL_INFO,
        visibility=TerminalVisibility.HIDDEN,
        backend_factory=_FakeBackend,
        storage_dir=tmp_path,
    )
    await mgr2.load_state()
    assert "alpha" in mgr2.list_names()


def test_save_state_returns_early_when_storage_dir_is_none() -> None:
    mgr = BaseTerminalManager(
        shell_info=_TEST_SHELL_INFO,
        visibility=TerminalVisibility.HIDDEN,
        backend_factory=_FakeBackend,
    )
    assert mgr._store is None
