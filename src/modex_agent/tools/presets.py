"""Tool preset definitions for subagent tool registration."""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import Final

from modex_agent.core.tool_manager import Tool
from modex_agent.tools.manager import InMemoryToolManager
from modex_agent.tools.standard import (
    EditFileTool,
    GlobTool,
    ListDirTool,
    ReadFileTool,
    SearchFilesTool,
    WriteFileTool,
)
from modex_agent.tools.workspace_scoped import WorkspaceRootProvider, wrap_standard_tools


class ToolPreset(StrEnum):
    """Declarative tool preset for subagent assignment.

    Values map to tool factory lists in TOOL_PRESETS.
    """

    FULL = "full"  # all tools + bash + terminal
    READ_WRITE = "read_write"  # read + write + edit + grep/glob + bash (review & fix)
    READ_ONLY = "read_only"  # read + grep/glob + bash (prompt-constrained read-only)
    NONE = "none"  # no standard tools — communication tools only (MCP still loaded)
    WEB = "web"  # web search + web reader (opt-in, not included in FULL)


class ContextMode(StrEnum):
    """Subagent context mode — controls memory inheritance strategy."""

    FRESH = "fresh"  # clean session, no parent context inherited
    FORK = "fork"  # system-prompt injection of truncated parent context as read-only reference


class ThinkingBudget(StrEnum):
    """Thinking budget annotation for subagent LLM calls."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


# Fork-context truncation bounds (only meaningful when context_mode == FORK).
# Centralized so the AgentTemplate default, the bot payload schema, and the
# registry loader share one source of truth.
DEFAULT_FORK_MAX_MESSAGES: int = 80
MAX_FORK_MAX_MESSAGES: int = 100


def _make_standard_read() -> list[Tool]:
    """Create read-only standard tools."""
    return [ReadFileTool(), ListDirTool(), SearchFilesTool(), GlobTool()]


def _make_standard_read_write() -> list[Tool]:
    """Create read+write standard tools (no bash)."""
    return [
        ReadFileTool(),
        WriteFileTool(),
        EditFileTool(),
        ListDirTool(),
        SearchFilesTool(),
        GlobTool(),
    ]


def _make_standard_full() -> list[Tool]:
    """Create full standard tools (bash registered separately)."""
    return [
        ReadFileTool(),
        WriteFileTool(),
        EditFileTool(),
        ListDirTool(),
        SearchFilesTool(),
        GlobTool(),
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
        scoped_write_dir: Retained for potential future scoped-write needs;
            currently no caller (subagent deliverable is now reply-text-based).
            If provided and the preset lacks native write capability
            (READ_ONLY, NONE), a ScopedWriteFileTool restricted to this directory
            is injected.
        root_provider: If provided, standard tools are wrapped so their relative
            paths resolve against the workspace root instead of process CWD.

    Returns:
        List of Tool instances ready for registration.
    """
    tool_lists: dict[ToolPreset, Callable[[], list[Tool]]] = {
        ToolPreset.FULL: _make_standard_full,
        ToolPreset.READ_WRITE: _make_standard_read_write,
        ToolPreset.READ_ONLY: _make_standard_read,
        ToolPreset.NONE: _make_standard_none,
        ToolPreset.WEB: _make_web_tools,
    }

    factory = tool_lists[preset]
    tools: list[Tool] = factory()

    # Wrap standard tools with workspace root provider when given
    if root_provider is not None:
        tools = wrap_standard_tools(tools, root_provider)

    # Presets without native write/edit: inject scoped tools.
    # Retained for potential future scoped-write needs; currently no caller
    # (subagent deliverable is now reply-text-based).
    if scoped_write_dir is not None and preset in (ToolPreset.READ_ONLY, ToolPreset.NONE):
        from modex_agent.memory.tools.scoped_edit import ScopedEditFileTool
        from modex_agent.memory.tools.scoped_write import ScopedWriteFileTool

        scoped = [scoped_write_dir]
        tools.append(ScopedWriteFileTool(allowed_dirs=scoped))
        tools.append(ScopedEditFileTool(allowed_dirs=scoped))

    # Bash tool: FULL, READ_ONLY, and READ_WRITE get bash; NONE does not
    if subprocess_tool_factory is not None and preset in (
        ToolPreset.FULL,
        ToolPreset.READ_ONLY,
        ToolPreset.READ_WRITE,
    ):
        bash_tool = subprocess_tool_factory()
        if root_provider is not None:
            wrapped = wrap_standard_tools([bash_tool], root_provider)
            if not wrapped:
                raise RuntimeError("wrap_standard_tools returned empty list for bash tool")
            bash_tool = wrapped[0]
        tools.append(bash_tool)

    return tools


def build_preset_tool_manager(
    root_provider: WorkspaceRootProvider,
    preset: ToolPreset,
) -> InMemoryToolManager:
    """Build an InMemoryToolManager populated with the tools for a preset.

    Tools are wrapped with the given root_provider so relative paths resolve
    against the workspace root instead of process CWD.
    """
    tools = get_preset_tools(preset, root_provider=root_provider)
    manager = InMemoryToolManager()
    for tool in tools:
        manager.register(tool)
    return manager


#: Hook name bound to the experience capability's review hook (single
#: authority): ``plugins/defaults/hooks.py`` registers the review hook
#: under it and the ``experience`` capability package contributes it into
#: hook rosters.
EXPERIENCE_REVIEW_HOOK_NAME: Final = "experience_review"


def make_ast_grep_tools() -> list[Tool]:
    """Create the AST search/replace tool pair (registry names
    ``ast_grep_search`` / ``ast_grep_replace``).

    Consumer: ``plugins/defaults/tools.py`` registers the tools under
    their own names via the per-name builders; this paired face serves
    the tests that assert the pair's shape. Delegates to the per-name
    builders — one construction path.
    """
    return [make_ast_grep_search_tool(), make_ast_grep_replace_tool()]


def make_ast_grep_search_tool() -> Tool:
    """Create the AST search tool (registry name ``ast_grep_search``).

    Single-tool face for per-name registration (``PrototypeFactory``
    builders are one tool each).
    """
    from modex_agent.tools.ast import AstGrepSearchTool

    return AstGrepSearchTool()


def make_ast_grep_replace_tool() -> Tool:
    """Create the AST replace tool (registry name ``ast_grep_replace``).

    Single-tool face for per-name registration (``PrototypeFactory``
    builders are one tool each).
    """
    from modex_agent.tools.ast import AstGrepReplaceTool

    return AstGrepReplaceTool()


def make_aci_edit_tool() -> Tool:
    """Create the ACI-enhanced edit tool (registry name ``aci_edit``).

    Produces one ``AciEditTool`` (LLM-facing name ``"edit"``) with
    post-edit lint feedback — a drop-in upgrade of ``EditFileTool``.
    Consumer: ``plugins/defaults/tools.py`` registers the instance under
    the ``aci_edit`` registry name; the ``aci`` capability package
    (``plugins/defaults/capabilities/aci.py``) contributes that name into
    rosters with the ``edit ← aci_edit`` O3 replacement.
    """
    from modex_agent.tools.aci.edit_tool import AciEditTool
    from modex_agent.tools.lint import default_lint_registry

    return AciEditTool(default_lint_registry)
