"""BaseTerminalManager — three capability flags are Optional and default-off."""

from __future__ import annotations

from pathlib import Path

from modex_agent.tools.terminal.managers import BaseTerminalManager
from modex_agent.tools.terminal.types import Platform, ShellFamily, ShellInfo, TerminalVisibility


class _FakeBackend:
    platform = Platform.LINUX
    visibility = TerminalVisibility.HIDDEN


def _shell_info() -> ShellInfo:
    return ShellInfo(family=ShellFamily.BASH, path="/bin/bash", platform=Platform.LINUX)


def test_base_manager_lean_form_has_no_lru_persistence_memory_pressure() -> None:
    mgr = BaseTerminalManager(
        shell_info=_shell_info(),
        visibility=TerminalVisibility.HIDDEN,
        backend_factory=_FakeBackend,
    )
    assert mgr._max_terminals is None
    assert mgr._storage_dir is None
    assert mgr._enable_memory_pressure is False


def test_base_manager_accepts_three_capability_flags() -> None:
    mgr = BaseTerminalManager(
        shell_info=_shell_info(),
        visibility=TerminalVisibility.HIDDEN,
        backend_factory=_FakeBackend,
        max_terminals=4,
        storage_dir=Path("/tmp/foo"),
        enable_memory_pressure=True,
    )
    assert mgr._max_terminals == 4
    assert mgr._storage_dir == Path("/tmp/foo")
    assert mgr._enable_memory_pressure is True
