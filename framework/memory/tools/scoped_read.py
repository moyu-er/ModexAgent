"""Scoped file-read tool that validates paths against an allowed-dirs whitelist."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from framework.core.tool_manager import Tool, ToolResult
from framework.memory.tools._utils import validate_scoped_path


class ScopedReadFileTool(Tool):
    """Read a file within allowed directories."""

    def __init__(self, allowed_dirs: list[Path]) -> None:
        self._allowed_dirs = [d.resolve() for d in allowed_dirs]
        allowed_list = "\n".join(f"  - {d}" for d in self._allowed_dirs)
        super().__init__(
            name="read",
            description=(
                "Read the content of a file.\n\n"
                "You can ONLY read files under these directories:\n"
                f"{allowed_list}"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path to the file to read",
                    },
                },
                "required": ["path"],
            },
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        raw_path = kwargs.get("path", "")
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
            return ToolResult(tool_name=self.name, result=content)
        except Exception as exc:
            return ToolResult(tool_name=self.name, error=str(exc))
