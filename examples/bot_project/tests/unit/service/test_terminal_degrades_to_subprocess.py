"""Terminal degradation to SubprocessTool when no bash backend is available.

When ``_build_terminal_manager`` cannot find a supported shell or every backend
fails to start, the bot must still work by registering ``SubprocessTool`` as the
``bash`` tool and omitting ``terminal`` / ``process`` entirely.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parents[3]))

from bot.service.pool_builder import _build_terminal_manager, _build_tools  # noqa: E402
from modex_agent.ioc.configs.pool import MediaConfig  # noqa: E402
from modex_agent.tools.terminal.subprocess_tool import SubprocessTool  # noqa: E402


def _pool_cfg(use_terminal: bool = True, visibility: bool = True) -> object:
    return SimpleNamespace(
        agents=[
            SimpleNamespace(
                role="main",
                use_terminal=use_terminal,
                terminal_visibility=visibility,
            )
        ]
    )


def test_terminal_manager_degrades_to_none_when_no_shell() -> None:
    """No supported shell → terminal manager is None, bot falls back."""
    with patch("bot.service.pool_builder.detect_platform_shell", return_value=None):
        assert _build_terminal_manager(_pool_cfg(), "p", None) is None


def test_terminal_manager_degrades_to_none_when_all_backends_fail() -> None:
    """Shell exists but every backend fails → still None."""
    with patch(
        "bot.service.pool_builder.create_terminal_manager",
        side_effect=RuntimeError("no backend"),
    ):
        assert _build_terminal_manager(_pool_cfg(), "p", None) is None


@pytest.mark.asyncio
async def test_tools_degrade_to_subprocess_when_terminal_manager_none() -> None:
    """With terminal_manager=None, bash is SubprocessTool; terminal/process absent."""
    tm, _mcp, _todo = await _build_tools(
        pool_cfg=SimpleNamespace(mcp=None, media=MediaConfig()),
        main_cfg=SimpleNamespace(experience=None),
        terminal_manager=None,
        project_dir=Path("."),
        output_adapter=SimpleNamespace(send=lambda *a, **k: None),
        pool_name="test-pool",
        data_dir=Path("."),
        pool_data=None,
        root_provider=None,
    )

    tool_names = tm.list_tools()
    assert "bash" in tool_names
    bash_tool = tm.get_tool("bash")
    assert isinstance(bash_tool, SubprocessTool)

    assert "terminal" not in tool_names
    assert "process" not in tool_names
