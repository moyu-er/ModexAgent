"""Terminal config is read from the pool's main-agent ``AgentConfig`` (inline
in ``config/pools/<pool>/pool.yml`` ``agents:`` block), not from a template.

``_build_terminal_manager`` reads ``use_terminal`` / ``terminal_visibility``
off ``pool_cfg.agents`` (the role='main' entry). This keeps the
terminal/process/command tools main-branch-style: configuring
``use_terminal: true`` on the main agent in pool.yml is all that's needed, for
ANY pool (main, coding, research, ...).
"""
from __future__ import annotations

from types import SimpleNamespace


def _pool_cfg(use_terminal: bool, terminal_visibility: bool = True, name: str = "main"):
    return SimpleNamespace(
        agents=[
            SimpleNamespace(
                role="main",
                name=name,
                use_terminal=use_terminal,
                terminal_visibility=terminal_visibility,
            )
        ]
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

    mgr = pool_builder._build_terminal_manager(_pool_cfg(True), "main", None)
    assert mgr is sentinel  # use_terminal=True on the main agent -> created


def test_build_terminal_manager_skips_when_main_agent_opts_out(monkeypatch):
    from bot.service import pool_builder

    attempted = {"n": 0}

    def boom(**kw):
        attempted["n"] += 1
        return object()

    monkeypatch.setattr(pool_builder, "detect_platform_shell", lambda: SimpleNamespace())
    monkeypatch.setattr(pool_builder, "create_terminal_manager", boom)

    mgr = pool_builder._build_terminal_manager(_pool_cfg(False), "coding", None)
    assert mgr is None  # use_terminal=False -> skipped
    assert attempted["n"] == 0  # backend never probed


def test_build_terminal_manager_works_for_any_pool_main_agent(monkeypatch):
    """The terminal config is keyed on EACH pool's role='main' agent, not on a
    pool literally named 'main'. A pool whose main agent 'researcher' sets
    use_terminal=true gets terminal tools just the same."""
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
        _pool_cfg(True, terminal_visibility=False, name="researcher"), "research", None
    )
    assert mgr is sentinel
    # terminal_visibility=False is honored (HIDDEN preferred over VISIBLE).
    from modex_agent.tools.terminal.types import TerminalVisibility

    assert seen["visibility"] is TerminalVisibility.HIDDEN
