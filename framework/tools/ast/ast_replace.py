"""ast_grep_replace tool — replace code using AST pattern matching (tree-sitter S-expression queries)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from framework.core.tool_manager import Tool
from framework.tools.ast.engine import (
    AST_UNAVAILABLE_MSG,
    AstNotAvailableError,
    AstParseError,
    AstQueryError,
    is_ast_available,
    replace_in_file,
)


class AstGrepReplaceTool(Tool):
    """Replace code using AST-aware pattern matching. Dry-run by default."""

    def __init__(self) -> None:
        super().__init__()

    @property
    def name(self) -> str:
        return "ast_grep_replace"

    @property
    def description(self) -> str:
        return (
            "Replace code using AST pattern matching. "
            "Each match of the S-expression pattern is replaced with the replacement text. "
            "Use @capture_name to identify nodes in the pattern. "
            "Dry-run by default — set dry_run=false to apply changes."
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
                        "Example: '(function_definition name: (identifier) @name) @func'"
                    ),
                },
                "replacement": {
                    "type": "string",
                    "description": "Text to replace each full pattern match with.",
                },
                "language": {
                    "type": "string",
                    "description": "Programming language: 'python' or 'java'",
                    "enum": ["python", "java"],
                },
                "path": {
                    "type": "string",
                    "description": "Target file path (required, must be a single file)",
                },
                "dry_run": {
                    "type": "boolean",
                    "description": "Preview changes without writing (default: true)",
                },
            },
            "required": ["pattern", "replacement", "language", "path"],
        }

    async def execute(
        self,
        pattern: str,
        replacement: str,
        language: str,
        path: str,
        dry_run: bool = True,
        **kwargs: object,
    ) -> str:
        if not is_ast_available():
            return AST_UNAVAILABLE_MSG

        file_path = Path(path)
        if not file_path.is_file():
            return f"Error: {path} is not a file. ast_grep_replace requires a single file path."

        try:
            source = file_path.read_text(encoding="utf-8")
            new_source, count = replace_in_file(source, pattern, replacement, language)

            if count == 0:
                return "No matches found. Nothing to replace."

            if dry_run:
                # Show diff-like preview
                lines: list[str] = [f"--- {file_path.name}"]
                old_lines = source.split("\n")
                new_lines = new_source.split("\n")
                for i, (old, new) in enumerate(zip(old_lines, new_lines)):
                    if old != new:
                        lines.append(f"- {old}")
                        lines.append(f"+ {new}")
                lines.append(f"\n{count} replacement(s) (dry run). Set dry_run=false to apply.")
                return "\n".join(lines)

            file_path.write_text(new_source, encoding="utf-8")
            return f"{count} replacement(s) applied to {file_path.name}."

        except AstNotAvailableError as e:
            return str(e)
        except Exception as e:
            return f"Error: {e}"
