"""Scoped directory-listing tool that validates paths against an allowed-dirs whitelist."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from framework.core.tool_manager import Tool, ToolResult
from framework.memory.tools._utils import validate_scoped_path


class ScopedListTool(Tool):
    """List directory contents within allowed directories."""

    def __init__(self, allowed_dirs: list[Path]) -> None:
        self._allowed_dirs = [d.resolve() for d in allowed_dirs]
        super().__init__(
            name="ls",
            description="List the contents of a directory.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path to the directory to list",
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
                error=f"Directory not found: {resolved}",
            )
        if not resolved.is_dir():
            return ToolResult(
                tool_name=self.name,
                error=f"Not a directory: {resolved}",
            )
        try:
            entries: list[str] = []
            for child in sorted(resolved.iterdir(), key=lambda p: p.name):
                kind = "dir" if child.is_dir() else "file"
                entries.append(f"  {kind}  {child.name}")
            result = "\n".join(entries) if entries else "(empty directory)"
            return ToolResult(tool_name=self.name, result=result)
        except Exception as exc:
            return ToolResult(tool_name=self.name, error=str(exc))
