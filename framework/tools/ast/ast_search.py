"""ast_grep_search tool — search code using AST pattern matching."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from framework.core.tool_manager import Tool
from framework.tools.ast.engine import (
    AST_UNAVAILABLE_MSG,
    AstNotAvailableError,
    is_ast_available,
    search_in_file,
    search_in_directory,
)

_EXT_MAP: dict[str, tuple[str, ...]] = {
    "python": (".py",),
    "java": (".java",),
}


class AstGrepSearchTool(Tool):
    """Search code using AST-aware pattern matching."""

    def __init__(self) -> None:
        super().__init__()

    @property
    def name(self) -> str:
        return "ast_grep_search"

    @property
    def description(self) -> str:
        return (
            "Search code using AST pattern matching. "
            "Use $NAME for a single node, $$$ARGS for zero or more nodes. "
            "Example: 'def $FUNC($$$ARGS): return $EXPR' matches function definitions. "
            "Supported languages: python, java."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": (
                        "AST pattern. $NAME captures a single AST node. "
                        "$$$ARGS captures zero or more nodes. "
                        "Example: 'function $NAME($$$ARGS) { $$$BODY }'"
                    ),
                },
                "language": {
                    "type": "string",
                    "description": "Programming language: 'python' or 'java'",
                    "enum": ["python", "java"],
                },
                "path": {
                    "type": "string",
                    "description": "File or directory to search (default: current working directory)",
                },
            },
            "required": ["pattern", "language"],
        }

    async def execute(
        self,
        pattern: str,
        language: str,
        path: str | None = None,
        **kwargs: object,
    ) -> str:
        if not is_ast_available():
            return AST_UNAVAILABLE_MSG

        search_path = Path(path) if path else Path.cwd()

        try:
            if search_path.is_file():
                source = search_path.read_text(encoding="utf-8")
                matches = search_in_file(source, pattern, language, str(search_path))
            else:
                exts = _EXT_MAP.get(language, (".py",))
                matches = search_in_directory(search_path, pattern, language, exts)

            if not matches:
                return "No matches found."

            lines: list[str] = []
            for m in matches:
                lines.append(f"{m.file_path}:{m.line}:{m.column}: {m.text}")

            lines.append(f"\nFound {len(matches)} match(es).")
            return "\n".join(lines)

        except AstNotAvailableError as e:
            return str(e)
        except Exception as e:
            return f"Error: {e}"
