"""Tool-subset declaration vocabulary shared by scope and tools (W1 layering surgery).

``ToolPreset`` names a declarative tool subset: the scope declarations
(``AgentSpec.toolset``) and profile/overlay layers read it, while the tools
package expands it into concrete tool factories
(:func:`~modex_agent.tools.presets.get_preset_tools`). ``ContextMode`` and
the fork-context bounds are the matching subagent context-inheritance knobs.
scope and tools sit at the SAME layering level, so core is the only home
legal for both readers.
"""

from __future__ import annotations

from enum import StrEnum


class ToolPreset(StrEnum):
    """Declarative tool preset for subagent assignment.

    Values map to tool factory lists in TOOL_PRESETS.
    """

    FULL = "full"  # all scalar standard tools
    READ_WRITE = "read_write"  # read + write + edit + grep/glob
    READ_ONLY = "read_only"  # read + grep/glob
    NONE = "none"  # no standard tools — communication tools only (MCP still loaded)
    WEB = "web"  # web search + web reader (opt-in, not included in FULL)


class ContextMode(StrEnum):
    """Subagent context mode — controls memory inheritance strategy."""

    FRESH = "fresh"  # clean session, no parent context inherited
    FORK = "fork"  # system-prompt injection of truncated parent context as read-only reference


# Fork-context truncation bounds (only meaningful when context_mode == FORK).
# Centralized so the AgentTemplate default, the bot payload schema, and the
# registry loader share one source of truth.
DEFAULT_FORK_MAX_MESSAGES: int = 80
MAX_FORK_MAX_MESSAGES: int = 100


# ---------------------------------------------------------------------------
# Preset → tool-name expansion (static).
#
# The declarative name table scope derivation reads (W3b): expanding real
# Tool objects just to read their names dragged scope onto tools, so the
# closed name sets live here next to the ToolPreset enum they expand.
# ``tools/presets.py`` remains the single construction authority for the
# Tool instances; ``tests/unit/scope/test_compiler.py`` asserts the two
# never drift.
# ---------------------------------------------------------------------------
PRESET_TOOL_NAMES: dict[ToolPreset, tuple[str, ...]] = {
    ToolPreset.FULL: ("read", "write", "edit", "ls", "grep", "glob"),
    ToolPreset.READ_WRITE: ("read", "write", "edit", "ls", "grep", "glob"),
    ToolPreset.READ_ONLY: ("read", "ls", "grep", "glob"),
    ToolPreset.NONE: (),
    ToolPreset.WEB: ("web_search", "web_reader"),
}

# Delegation / peer-communication tool names shared by the pool runtime and
# turn-context assembly (formerly ``multi_agent/tools.py``).
SEND_TO_PEER_TOOL_NAME: str = "send_to_peer"
