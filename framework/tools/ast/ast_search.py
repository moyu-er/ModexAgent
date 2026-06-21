"""ast_grep_search tool — search code using AST pattern matching (tree-sitter S-expression queries)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from framework.core.tool_manager import Tool
from framework.workspace.runtime import resolve_workspace_root
from framework.tools.ast.engine import (
    _EXT_MAP,
    AST_UNAVAILABLE_MSG,
    AstNotAvailableError,
    is_ast_available,
    search_in_directory,
    search_in_file,
)


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
            "Search code using tree-sitter AST pattern matching. "
            "Use S-expression queries with @capture_name for captures. "
            "Example: '(function_definition name: (identifier) @name) @func' "
            "matches function definitions and captures names. "
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
                        "tree-sitter S-expression query pattern. "
                        "Use @capture_name to capture nodes. "
                        "Example: '(function_definition name: (identifier) @name)'"
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

        search_path = Path(path) if path else resolve_workspace_root()

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
