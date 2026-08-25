"""``create_terminal_manager_or_none`` — the platform/backend fallback ladder.

Migrated from the BIZ ``builders._build_terminal_manager`` (scope-assembly
ticket 05): the ladder is real platform logic (shell detection, VISIBLE/HIDDEN
attempt order, SubprocessTool-only fallback) and lives next to
``create_terminal_manager``. ``use_terminal`` governs exactly one thing —
whether the manager (infrastructure) is built; the trio tools are roster
opt-ins.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from modex_agent.tools.terminal.managers import create_terminal_manager_or_none
from modex_agent.tools.terminal.types import TerminalVisibility


def _shell() -> SimpleNamespace:
    return SimpleNamespace(family=SimpleNamespace(value="bash"))


def test_use_terminal_false_builds_no_manager() -> None:
    with patch(
        "modex_agent.tools.terminal.managers.detect_platform_shell"
    ) as detect:
        assert create_terminal_manager_or_none(
            use_terminal=False,
            terminal_visibility=True,
            pool_name="p",
        ) is None
        detect.assert_not_called()


def test_manager_created_when_main_agent_opts_in() -> None:
    sentinel = object()
    seen: dict[str, object] = {}

    def fake(**kw: object) -> object:
        seen.update(kw)
        return sentinel

    with (
        patch(
            "modex_agent.tools.terminal.managers.detect_platform_shell",
            return_value=_shell(),
        ),
        patch(
            "modex_agent.tools.terminal.managers.create_terminal_manager",
            side_effect=fake,
        ),
    ):
        mgr = create_terminal_manager_or_none(
            use_terminal=True,
            terminal_visibility=True,
            pool_name="main",
        )
    assert mgr is sentinel
    assert seen["visibility"] is TerminalVisibility.VISIBLE


def test_terminal_visibility_false_requests_hidden_only() -> None:
    sentinel = object()
    seen: dict[str, object] = {}

    def fake(**kw: object) -> object:
        seen.update(kw)
        return sentinel

    with (
        patch(
            "modex_agent.tools.terminal.managers.detect_platform_shell",
            return_value=_shell(),
        ),
        patch(
            "modex_agent.tools.terminal.managers.create_terminal_manager",
            side_effect=fake,
        ),
    ):
        mgr = create_terminal_manager_or_none(
            use_terminal=True,
            terminal_visibility=False,
            pool_name="research",
        )
    assert mgr is sentinel
    assert seen["visibility"] is TerminalVisibility.HIDDEN


def test_no_supported_shell_degrades_to_none() -> None:
    with patch(
        "modex_agent.tools.terminal.managers.detect_platform_shell",
        return_value=None,
    ):
        assert create_terminal_manager_or_none(
            use_terminal=True,
            terminal_visibility=True,
            pool_name="p",
        ) is None


def test_all_backends_failed_degrades_to_none() -> None:
    with (
        patch(
            "modex_agent.tools.terminal.managers.detect_platform_shell",
            return_value=_shell(),
        ),
        patch(
            "modex_agent.tools.terminal.managers.create_terminal_manager",
            side_effect=RuntimeError("no backend"),
        ),
    ):
        assert create_terminal_manager_or_none(
            use_terminal=True,
            terminal_visibility=True,
            pool_name="p",
        ) is None


def test_visible_failure_falls_back_to_hidden() -> None:
    """The VISIBLE->HIDDEN fallback attempt order survives the migration."""
    attempted: list[TerminalVisibility] = []
    sentinel = object()

    def fake(*, visibility: TerminalVisibility, **_kw: object) -> object:
        attempted.append(visibility)
        if visibility is TerminalVisibility.VISIBLE:
            raise RuntimeError("visible backend unavailable")
        return sentinel

    with (
        patch(
            "modex_agent.tools.terminal.managers.detect_platform_shell",
            return_value=_shell(),
        ),
        patch(
            "modex_agent.tools.terminal.managers.create_terminal_manager",
            side_effect=fake,
        ),
    ):
        mgr = create_terminal_manager_or_none(
            use_terminal=True,
            terminal_visibility=True,
            pool_name="p",
            default_cwd="/tmp/ws",
        )
    assert mgr is sentinel
    assert attempted == [TerminalVisibility.VISIBLE, TerminalVisibility.HIDDEN]
