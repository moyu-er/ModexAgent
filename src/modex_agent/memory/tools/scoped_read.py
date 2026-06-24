"""Scoped file-read tool that validates paths against an allowed-dirs whitelist."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from modex_agent.core.tool_manager import Tool, ToolResult
from modex_agent.memory.tools._utils import validate_scoped_path
from modex_agent.tools.standard.file_tool import _DEFAULT_LIMIT, _paginate_file


class ScopedReadFileTool(Tool):
    """Read a file within allowed directories, with pagination support."""

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
                    "offset": {
                        "type": "integer",
                        "description": "Number of lines to skip from the beginning (0-based, default: 0)",
                        "default": 0,
                    },
                    "limit": {
                        "type": "integer",
                        "description": f"Maximum number of lines to read (default: {_DEFAULT_LIMIT})",
                        "default": _DEFAULT_LIMIT,
                    },
                },
                "required": ["path"],
            },
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        raw_path = kwargs.get("path", "")
        offset = kwargs.get("offset", 0)
        limit = kwargs.get("limit", _DEFAULT_LIMIT)

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
            result = _paginate_file(resolved, offset=offset, limit=limit)
            # 检查是否为错误返回
            if result.startswith("Error:"):
                return ToolResult(tool_name=self.name, error=result)
            return ToolResult(tool_name=self.name, result=result)
        except Exception as exc:
            return ToolResult(tool_name=self.name, error=str(exc))
