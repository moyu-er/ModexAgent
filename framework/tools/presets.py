"""Tool preset definitions for subagent tool registration."""

from __future__ import annotations

from collections.abc import Callable
from enum import Enum

from framework.core.tool_manager import Tool
from framework.tools.standard import (
    EditFileTool,
    FindFilesTool,
    ListDirTool,
    ReadFileTool,
    SearchFilesTool,
    WriteFileTool,
)


class ToolPreset(str, Enum):
    """Declarative tool preset for subagent assignment.

    Values map to tool factory lists in TOOL_PRESETS.
    """
    FULL = "full"               # all tools + bash + terminal
    READ_WRITE = "read_write"   # read + write + edit + search (no bash)
    READ_ONLY = "read_only"     # read + search + bash (prompt-constrained read-only)
    MINIMAL = "minimal"         # read + write + list (no edit, no bash)


def _make_standard_read() -> list[Tool]:
    """Create read-only standard tools."""
    return [ReadFileTool(), ListDirTool(), SearchFilesTool(), FindFilesTool()]


def _make_standard_read_write() -> list[Tool]:
    """Create read+write standard tools (no bash)."""
    return [
        ReadFileTool(), WriteFileTool(), EditFileTool(),
        ListDirTool(), SearchFilesTool(), FindFilesTool(),
    ]


def _make_standard_full() -> list[Tool]:
    """Create full standard tools (bash registered separately)."""
    return [
        ReadFileTool(), WriteFileTool(), EditFileTool(),
        ListDirTool(), SearchFilesTool(), FindFilesTool(),
    ]


def _make_standard_minimal() -> list[Tool]:
    """Create minimal tools (read + write + search, no edit, no bash)."""
    return [
        ReadFileTool(), WriteFileTool(), ListDirTool(), SearchFilesTool(),
    ]


def get_preset_tools(
    preset: ToolPreset,
    *,
    subprocess_tool_factory: Callable[[], Tool] | None = None,
) -> list[Tool]:
    """Return the list of tools for a preset.

    Args:
        preset: The tool preset enum value.
        subprocess_tool_factory: If provided, creates a bash tool (SubprocessTool or CommandTool).
                                 Always added to FULL and READ_ONLY presets.

    Returns:
        List of Tool instances ready for registration.
    """
    tool_lists: dict[ToolPreset, Callable[[], list[Tool]]] = {
        ToolPreset.FULL: _make_standard_full,
        ToolPreset.READ_WRITE: _make_standard_read_write,
        ToolPreset.READ_ONLY: _make_standard_read,
        ToolPreset.MINIMAL: _make_standard_minimal,
    }

    factory = tool_lists[preset]
    tools: list[Tool] = factory()

    # Bash tool: FULL and READ_ONLY get bash; READ_WRITE and MINIMAL do not
    if subprocess_tool_factory is not None and preset in (ToolPreset.FULL, ToolPreset.READ_ONLY):
        tools.append(subprocess_tool_factory())

    return tools
