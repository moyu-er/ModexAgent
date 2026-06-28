"""_build_terminal_manager — ADR-0010 two-axis construction with fallback.

The YAML-visible ``terminal_visibility`` bool keeps its semantics (True = prefer
visible, False = prefer hidden). Internally the function maps to
``TerminalVisibility`` and calls the two-axis ``create_terminal_manager``.
Production also probes ``create_pty_backend`` eagerly so an unsupported
(transport, visibility) combo is surfaced at pool-build time (ADR-0010
Consequences), so both factory calls must be mocked here.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

# Bot tests resolve ``bot.*`` via the repo root inserted into sys.path.
sys.path.insert(0, str(Path(__file__).parents[3]))

from bot.service.pool_builder import _build_terminal_manager  # noqa: E402
from modex_agent.tools.terminal.backends.factory import (  # noqa: E402
    UnsupportedVisibilityForTransport,
)
from modex_agent.tools.terminal.types import TerminalVisibility  # noqa: E402


def _pool_cfg(visibilities: list[bool]) -> object:
    agents = [
        SimpleNamespace(role="main", use_terminal=True, terminal_visibility=v)
        for v in visibilities
    ]
    return SimpleNamespace(agents=agents)


def test_returns_none_when_no_agent_enables_terminal() -> None:
    pool_cfg = SimpleNamespace(
        agents=[SimpleNamespace(role="main", use_terminal=False, terminal_visibility=False)]
    )
    assert _build_terminal_manager(pool_cfg, "p", None) is None


def test_uses_two_axis_constructor_when_terminal_enabled() -> None:
    """visibility=True → prefer VISIBLE; probe succeeds; manager returned."""
    pool_cfg = _pool_cfg([True])
    sentinel = object()
    with patch(
        "bot.service.pool_builder.create_terminal_manager"
    ) as mock_factory, patch(
        "bot.service.pool_builder.create_pty_backend"
    ) as mock_probe:
        mock_factory.return_value = sentinel
        mock_probe.return_value = None  # probe succeeds
        result = _build_terminal_manager(pool_cfg, "p", None)
    assert result is sentinel
    kwargs = mock_factory.call_args.kwargs
    assert "shell_family" in kwargs
    assert kwargs["visibility"] == TerminalVisibility.VISIBLE


def test_falls_back_when_visible_unavailable() -> None:
    """VISIBLE probe raises UnsupportedVisibilityForTransport → retry HIDDEN."""
    pool_cfg = _pool_cfg([True])
    sentinel = object()

    def _probe(**kwargs):  # type: ignore[no-untyped-def]
        vis = kwargs["visibility"]
        if vis == TerminalVisibility.VISIBLE:
            raise UnsupportedVisibilityForTransport("no visible on this transport")
        return None  # HIDDEN probe succeeds

    with patch(
        "bot.service.pool_builder.create_terminal_manager"
    ) as mock_factory, patch(
        "bot.service.pool_builder.create_pty_backend",
        side_effect=_probe,
    ):
        mock_factory.return_value = sentinel
        result = _build_terminal_manager(pool_cfg, "p", None)
    assert result is sentinel
    assert mock_factory.call_args.kwargs["visibility"] == TerminalVisibility.HIDDEN
