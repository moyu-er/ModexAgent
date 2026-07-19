"""Terminal degradation to SubprocessTool when no bash backend is available.

When ``_build_terminal_manager`` cannot find a supported shell or every backend
fails to start, the bot must still work by registering ``SubprocessTool`` as the
``bash`` tool and omitting ``terminal`` / ``process`` entirely.

Ticket 6: the helpers moved from ``pool_builder`` into the shared
:class:`_PoolAssemblyMixin` (inherited by ``ReactExecutionStrategy``). Patches
target ``bot.service._assembly_helpers`` and the helpers are invoked via a
``ReactExecutionStrategy`` instance.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parents[3]))

from bot.service.react_strategy import ReactExecutionStrategy  # noqa: E402
from modex_agent.multi_agent.pool_config.deps import PoolAssemblyDeps  # noqa: E402
from modex_agent.multi_agent.pool_config.specs import MainAgentSpec  # noqa: E402
from modex_agent.tools.presets import ToolPreset  # noqa: E402
from modex_agent.tools.terminal.subprocess_tool import SubprocessTool  # noqa: E402


def _main_spec(use_terminal: bool = True, visibility: bool = True) -> MainAgentSpec:
    return MainAgentSpec(
        agent_name="main",
        use_terminal=use_terminal,
        terminal_visibility=visibility,
    )


def test_terminal_manager_degrades_to_none_when_no_shell() -> None:
    strategy = ReactExecutionStrategy()
    with patch("bot.service._assembly_helpers.detect_platform_shell", return_value=None):
        assert strategy._build_terminal_manager(_main_spec(), "p", None) is None


def test_terminal_manager_degrades_to_none_when_all_backends_fail() -> None:
    strategy = ReactExecutionStrategy()
    with patch(
        "bot.service._assembly_helpers.create_terminal_manager",
        side_effect=RuntimeError("no backend"),
    ):
        assert strategy._build_terminal_manager(_main_spec(), "p", None) is None


@pytest.mark.asyncio
async def test_tools_degrade_to_subprocess_when_terminal_manager_none() -> None:
    strategy = ReactExecutionStrategy()
    main_spec = MainAgentSpec(agent_name="main", tool_preset=ToolPreset.FULL)
    assembly_deps = PoolAssemblyDeps()
    tm, _mcp, _todo = await strategy._build_tools(
        main_spec=main_spec,
        assembly_deps=assembly_deps,
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
