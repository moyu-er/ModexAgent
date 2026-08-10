"""Capability flags for graph knowledge base tool, derived from agent's tools."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from modex_agent.tools.presets import ToolPreset

if TYPE_CHECKING:
    from modex_agent.core.tool_manager import ToolManager


class KnowledgeToolCapabilities(BaseModel):
    """What knowledge base actions an agent is allowed to use.

    Derived from the agent's actual tool set. READ capability (read/ls/grep)
    is granted when the agent has a read tool. WRITE/EDIT follow the agent's
    own write/edit tool availability.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    has_read: bool
    has_write: bool
    has_edit: bool

    @classmethod
    def from_tool_manager(cls, tool_manager: ToolManager) -> KnowledgeToolCapabilities:
        """Derive capabilities from the agent's registered tools."""
        names = set(tool_manager.list_tools())
        has_read = "read" in names
        has_write = "write" in names
        has_edit = "edit" in names
        if not has_read:
            has_read = any(n in names for n in ("read_file", "ls", "glob", "grep"))
        return cls(has_read=has_read, has_write=has_write, has_edit=has_edit)

    @classmethod
    def from_preset(cls, preset: ToolPreset) -> KnowledgeToolCapabilities:
        """Derive capabilities from a tool preset (legacy path)."""
        has_read = preset in (
            ToolPreset.FULL,
            ToolPreset.READ_WRITE,
            ToolPreset.READ_ONLY,
            ToolPreset.MINIMAL,
        )
        has_write = preset in (ToolPreset.FULL, ToolPreset.READ_WRITE, ToolPreset.MINIMAL)
        has_edit = preset in (ToolPreset.FULL, ToolPreset.READ_WRITE)
        return cls(has_read=has_read, has_write=has_write, has_edit=has_edit)

    def allowed_actions(self) -> list[str]:
        """Return the list of allowed action names for dynamic schema enum."""
        actions: list[str] = []
        if self.has_read:
            actions.extend(["read", "ls", "grep"])
        if self.has_write:
            actions.append("write")
        if self.has_edit:
            actions.append("edit")
        return actions


__all__ = ["KnowledgeToolCapabilities"]
