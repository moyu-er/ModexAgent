"""Scoped file-write tool that validates paths against an allowed-dirs whitelist."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from framework.core.tool_manager import Tool, ToolResult
from framework.memory.tools._utils import validate_scoped_path


class ScopedWriteFileTool(Tool):
    """Write a file within allowed directories."""

    def __init__(self, allowed_dirs: list[Path]) -> None:
        self._allowed_dirs = [d.resolve() for d in allowed_dirs]
        super().__init__(
            name="write",
            description="Write content to a file.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path to the file to write",
                    },
                    "content": {
                        "type": "string",
                        "description": "Content to write to the file",
                    },
                },
                "required": ["path", "content"],
            },
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        raw_path = kwargs.get("path", "")
        content = kwargs.get("content", "")
        try:
            resolved = validate_scoped_path(raw_path, self._allowed_dirs)
        except ValueError as exc:
            return ToolResult(tool_name=self.name, error=str(exc))

        try:
            resolved.parent.mkdir(parents=True, exist_ok=True)
            resolved.write_text(content, encoding="utf-8")
            return ToolResult(
                tool_name=self.name,
                result=f"Successfully wrote to {resolved}",
            )
        except Exception as exc:
            return ToolResult(tool_name=self.name, error=str(exc))
