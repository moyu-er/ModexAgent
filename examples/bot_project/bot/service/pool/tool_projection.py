"""Main-agent tool-name resolver (pure, for parity testing).

Extracted from ``pool_builder.py`` (ADR-0025 ticket 6 split). Pure projection
of the main-agent tool assembly (Task 1.6 parity helper).
"""

from __future__ import annotations

from typing import Any

from modex_agent.tools.presets import (
    ToolPreset,
    ToolSupplement,
    get_preset_tools,
    get_supplement_tools,
)
from modex_agent.tools.terminal import SubprocessTool, create_subprocess_executor


def build_main_agent_tool_names(
    tool_preset: str,
    supplements: list[str],
    use_terminal: bool,
) -> set[str]:
    """Return the set of tool NAMES the main agent will receive.

    Pure projection of the main-agent tool assembly (Task 1.6 parity
    helper). Mirrors :func:`_PoolAssemblyMixin._build_tools` + ``task``
    (subagent dispatch + peer communication):
    preset-gated file/search/bash + supplement tools (e.g. ast_grep) +
    terminal tools (when ``use_terminal``) + the always-on task tool.
    Bot-specific tools (send_file_to_user, todo, experience) and MCP tools
    are excluded from this projection - they are runtime/path-dependent and
    not governed by the preset/supplement policy.
    """
    names: set[str] = set()
    preset = ToolPreset(tool_preset)

    def _make_bash() -> Any:
        return SubprocessTool(executor=create_subprocess_executor(), timeout=90)

    # File/search/bash tool names per preset. The factory mirrors
    # _build_tools' _make_bash so the bash name surfaces for
    # FULL/READ_WRITE/READ_ONLY.
    for tool in get_preset_tools(preset, subprocess_tool_factory=_make_bash):
        names.add(tool.name)
    for tool in get_supplement_tools([ToolSupplement(s) for s in supplements]):
        names.add(tool.name)
    if use_terminal:
        # Real terminal tool names: CommandTool.name="bash" (already in names
        # via the preset factory above), ProcessTool.name="process",
        # TerminalTool.name="terminal".
        names |= {"bash", "process", "terminal"}
    names.add("task")
    return names
