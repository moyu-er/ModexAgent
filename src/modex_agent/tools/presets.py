"""Tool preset definitions for subagent tool registration."""

from __future__ import annotations

from collections.abc import Callable
from enum import Enum
from pathlib import Path

from modex_agent.tools.workspace_scoped import WorkspaceRootProvider, wrap_standard_tools
from modex_agent.core.tool_manager import Tool
from modex_agent.tools.standard import (
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

    FULL = "full"  # all tools + bash + terminal
    READ_WRITE = "read_write"  # read + write + edit + grep/find + bash (review & fix)
    READ_ONLY = "read_only"  # read + grep/find + bash (prompt-constrained read-only)
    MINIMAL = "minimal"  # read + write + list + grep (no edit, no bash)
    NONE = "none"  # no standard tools — communication tools only (MCP still loaded)
    WEB = "web"  # web search + web reader (opt-in, not included in FULL)


class ContextMode(str, Enum):
    """Subagent context mode — controls memory inheritance strategy."""

    FRESH = "fresh"  # clean session, no parent context inherited
    FORK = "fork"  # system-prompt injection of truncated parent context as read-only reference


class ThinkingBudget(str, Enum):
    """Thinking budget annotation for subagent LLM calls."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class SystemPromptMode(str, Enum):
    """System prompt assembly mode for subagent creation."""

    REPLACE = "replace"  # subagent uses its own complete prompt
    APPEND = "append"  # subagent prompt appended after parent's


# Fork-context truncation bounds (only meaningful when context_mode == FORK).
# Centralized so the AgentTemplate default, the bot payload schema, and the
# registry loader share one source of truth.
DEFAULT_FORK_MAX_MESSAGES: int = 80
MAX_FORK_MAX_MESSAGES: int = 100


def _make_standard_read() -> list[Tool]:
    """Create read-only standard tools."""
    return [ReadFileTool(), ListDirTool(), SearchFilesTool(), FindFilesTool()]


def _make_standard_read_write() -> list[Tool]:
    """Create read+write standard tools (no bash)."""
    return [
        ReadFileTool(),
        WriteFileTool(),
        EditFileTool(),
        ListDirTool(),
        SearchFilesTool(),
        FindFilesTool(),
    ]


def _make_standard_full() -> list[Tool]:
    """Create full standard tools (bash registered separately)."""
    return [
        ReadFileTool(),
        WriteFileTool(),
        EditFileTool(),
        ListDirTool(),
        SearchFilesTool(),
        FindFilesTool(),
    ]


def _make_standard_minimal() -> list[Tool]:
    """Create minimal tools (read + write + search, no edit, no bash)."""
    return [
        ReadFileTool(),
        WriteFileTool(),
        ListDirTool(),
        SearchFilesTool(),
    ]


def _make_standard_none() -> list[Tool]:
    """Create empty standard tool set (communication + MCP tools registered separately)."""
    return []


def _make_web_tools() -> list[Tool]:
    """Create web tools (web_search + web_reader)."""
    from modex_agent.tools.web.reader import WebReaderTool
    from modex_agent.tools.web.search import WebSearchTool

    return [WebSearchTool(), WebReaderTool()]


def get_preset_tools(
    preset: ToolPreset,
    *,
    subprocess_tool_factory: Callable[[], Tool] | None = None,
    scoped_write_dir: Path | None = None,
    root_provider: WorkspaceRootProvider | None = None,
) -> list[Tool]:
    """Return the list of tools for a preset.

    Args:
        preset: The tool preset enum value.
        subprocess_tool_factory: If provided, creates a bash tool (SubprocessTool or CommandTool).
        scoped_write_dir: If provided and the preset lacks native write capability
            (READ_ONLY, NONE), a ScopedWriteFileTool restricted to this directory
            is injected so the subagent can still write OUTPUT.md.
        root_provider: If provided, standard tools are wrapped so their relative
            paths resolve against the workspace root instead of process CWD.

    Returns:
        List of Tool instances ready for registration.
    """
    tool_lists: dict[ToolPreset, Callable[[], list[Tool]]] = {
        ToolPreset.FULL: _make_standard_full,
        ToolPreset.READ_WRITE: _make_standard_read_write,
        ToolPreset.READ_ONLY: _make_standard_read,
        ToolPreset.MINIMAL: _make_standard_minimal,
        ToolPreset.NONE: _make_standard_none,
        ToolPreset.WEB: _make_web_tools,
    }

    factory = tool_lists[preset]
    tools: list[Tool] = factory()

    # Wrap standard tools with workspace root provider when given
    if root_provider is not None:
        tools = wrap_standard_tools(tools, root_provider)

    # Presets without native write/edit: inject scoped tools for OUTPUT.md
    if scoped_write_dir is not None and preset in (ToolPreset.READ_ONLY, ToolPreset.NONE):
        from modex_agent.memory.tools.scoped_edit import ScopedEditFileTool
        from modex_agent.memory.tools.scoped_write import ScopedWriteFileTool

        scoped = [scoped_write_dir]
        tools.append(ScopedWriteFileTool(allowed_dirs=scoped))
        tools.append(ScopedEditFileTool(allowed_dirs=scoped))

    # Bash tool: FULL, READ_ONLY, and READ_WRITE get bash; MINIMAL and NONE do not
    if subprocess_tool_factory is not None and preset in (
        ToolPreset.FULL,
        ToolPreset.READ_ONLY,
        ToolPreset.READ_WRITE,
    ):
        bash_tool = subprocess_tool_factory()
        if root_provider is not None:
            wrapped = wrap_standard_tools([bash_tool], root_provider)
            if not wrapped:
                raise RuntimeError(
                    "wrap_standard_tools returned empty list for bash tool"
                )
            bash_tool = wrapped[0]
        tools.append(bash_tool)

    return tools


class ToolSupplement(str, Enum):
    """Additive tool group layered on top of a base ToolPreset.

    Unlike ToolPreset (one-of), supplements are multi-select and combine.
    """

    AST_GREP = "ast_grep"  # ast_grep_search + ast_grep_replace


def _make_ast_grep_tools() -> list[Tool]:
    from modex_agent.tools.ast import AstGrepReplaceTool, AstGrepSearchTool

    return [AstGrepSearchTool(), AstGrepReplaceTool()]


SUPPLEMENT_FACTORIES: dict[ToolSupplement, Callable[[], list[Tool]]] = {
    ToolSupplement.AST_GREP: _make_ast_grep_tools,
}


def get_supplement_tools(
    supplements: list[ToolSupplement],
    *,
    root_provider: WorkspaceRootProvider | None = None,
) -> list[Tool]:
    """Return deduped tool instances for the given additive supplements."""
    seen: set[str] = set()
    out: list[Tool] = []
    for sup in supplements:
        for tool in SUPPLEMENT_FACTORIES[sup]():
            if tool.name in seen:
                continue
            seen.add(tool.name)
            out.append(tool)
    if root_provider is not None and out:
        wrapped = wrap_standard_tools(out, root_provider)
        if not wrapped:
            raise RuntimeError("wrap_standard_tools returned empty for supplements")
        out = wrapped
    return out
