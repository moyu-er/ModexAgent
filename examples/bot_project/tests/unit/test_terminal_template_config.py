"""Terminal config is read from the pool's main-agent ``MainAgentSpec``.

``_build_terminal_manager`` reads ``use_terminal`` / ``terminal_visibility``
directly from ``main_spec``. The terminal/process/command tools are
main-branch-style: configuring ``use_terminal: true`` on the main agent
in pool.yml is all that's needed, for ANY pool.
"""
from __future__ import annotations

from types import SimpleNamespace


def _main_spec(use_terminal: bool, terminal_visibility: bool = True):
    return SimpleNamespace(
        use_terminal=use_terminal,
        terminal_visibility=terminal_visibility,
    )


def test_build_terminal_manager_creates_when_main_agent_opts_in(monkeypatch):
    from bot.service import pool_builder

    sentinel = object()
    monkeypatch.setattr(
        pool_builder,
        "detect_platform_shell",
        lambda: SimpleNamespace(family=SimpleNamespace(value="bash")),
    )
    monkeypatch.setattr(pool_builder, "create_terminal_manager", lambda **kw: sentinel)

    mgr = pool_builder._build_terminal_manager(_main_spec(True), "main", None)
    assert mgr is sentinel


def test_build_terminal_manager_skips_when_main_agent_opts_out(monkeypatch):
    from bot.service import pool_builder

    attempted = {"n": 0}

    def boom(**kw):
        attempted["n"] += 1
        return object()

    monkeypatch.setattr(pool_builder, "detect_platform_shell", lambda: SimpleNamespace())
    monkeypatch.setattr(pool_builder, "create_terminal_manager", boom)

    mgr = pool_builder._build_terminal_manager(_main_spec(False), "coding", None)
    assert mgr is None
    assert attempted["n"] == 0


def test_build_terminal_manager_works_for_any_pool_main_agent(monkeypatch):
    from bot.service import pool_builder

    sentinel = object()
    monkeypatch.setattr(
        pool_builder,
        "detect_platform_shell",
        lambda: SimpleNamespace(family=SimpleNamespace(value="bash")),
    )
    seen = {}

    def fake(**kw):
        seen.update(kw)
        return sentinel

    monkeypatch.setattr(pool_builder, "create_terminal_manager", fake)

    mgr = pool_builder._build_terminal_manager(
        _main_spec(True, terminal_visibility=False), "research", None
    )
    assert mgr is sentinel
    from modex_agent.tools.terminal.types import TerminalVisibility

    assert seen["visibility"] is TerminalVisibility.HIDDEN
