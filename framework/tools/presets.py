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
    READ_WRITE = "read_write"   # read + write + edit + grep/find + bash (review & fix)
    READ_ONLY = "read_only"     # read + grep/find + bash (prompt-constrained read-only)
    MINIMAL = "minimal"         # read + write + list + grep (no edit, no bash)
    NONE = "none"               # no standard tools — communication tools only (MCP still loaded)


class ContextMode(str, Enum):
    """Subagent context mode — controls memory inheritance strategy."""
    FRESH = "fresh"  # clean session, no parent context inherited
    FORK = "fork"    # system-prompt injection of truncated parent context as read-only reference


class ThinkingBudget(str, Enum):
    """Thinking budget annotation for subagent LLM calls."""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class SystemPromptMode(str, Enum):
    """System prompt assembly mode for subagent creation."""
    REPLACE = "replace"  # subagent uses its own complete prompt
    APPEND = "append"    # subagent prompt appended after parent's


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


def _make_standard_none() -> list[Tool]:
    """Create empty standard tool set (communication + MCP tools registered separately)."""
    return []


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
        ToolPreset.NONE: _make_standard_none,
    }

    factory = tool_lists[preset]
    tools: list[Tool] = factory()

    # Bash tool: FULL, READ_ONLY, and READ_WRITE get bash; MINIMAL and NONE do not
    if subprocess_tool_factory is not None and preset in (ToolPreset.FULL, ToolPreset.READ_ONLY, ToolPreset.READ_WRITE):
        tools.append(subprocess_tool_factory())

    return tools
