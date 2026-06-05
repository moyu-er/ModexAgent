"""Scoped file-edit tool that validates paths against an allowed-dirs whitelist."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from framework.core.tool_manager import Tool, ToolResult
from framework.memory.tools._utils import validate_scoped_path


class ScopedEditFileTool(Tool):
    """Edit a file by replacing text within allowed directories."""

    def __init__(self, allowed_dirs: list[Path]) -> None:
        self._allowed_dirs = [d.resolve() for d in allowed_dirs]
        super().__init__(
            name="edit",
            description="Edit a file by replacing old text with new text.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path to the file to edit",
                    },
                    "old_text": {
                        "type": "string",
                        "description": "Text to find and replace",
                    },
                    "new_text": {
                        "type": "string",
                        "description": "Replacement text",
                    },
                },
                "required": ["path", "old_text", "new_text"],
            },
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        raw_path = kwargs.get("path", "")
        old_text = kwargs.get("old_text", "")
        new_text = kwargs.get("new_text", "")
        try:
            resolved = validate_scoped_path(raw_path, self._allowed_dirs)
        except ValueError as exc:
            return ToolResult(tool_name=self.name, error=str(exc))

        if not resolved.exists():
            return ToolResult(
                tool_name=self.name,
                error=f"File not found: {resolved}",
            )
        if not resolved.is_file():
            return ToolResult(
                tool_name=self.name,
                error=f"Not a file: {resolved}",
            )
        try:
            content = resolved.read_text(encoding="utf-8")
            if old_text not in content:
                return ToolResult(
                    tool_name=self.name,
                    error=f"old_text not found in {resolved}",
                )
            updated = content.replace(old_text, new_text, 1)
            resolved.write_text(updated, encoding="utf-8")
            return ToolResult(
                tool_name=self.name,
                result=f"Successfully edited {resolved}",
            )
        except Exception as exc:
            return ToolResult(tool_name=self.name, error=str(exc))
