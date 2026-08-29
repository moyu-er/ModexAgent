"""lsp_navigation stub — LSP code navigation operations."""

from __future__ import annotations

from typing import Any

from modex_agent.core.tool_manager import Tool

_NOT_IMPLEMENTED = (
    "LSP navigation is not yet implemented. This tool will be available in a future update."
)


class LspNavigationTool(Tool):
    """Navigate code using Language Server Protocol (stub)."""

    def __init__(self) -> None:
        super().__init__()

    @property
    def name(self) -> str:
        return "lsp_navigation"

    @property
    def description(self) -> str:
        return (
            "Navigate code using LSP (Language Server Protocol). "
            "Operations: go_to_definition, find_references, hover, "
            "document_symbol, workspace_symbol, go_to_implementation, "
            "incoming_calls, outgoing_calls. "
            f"(Note: {_NOT_IMPLEMENTED})"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "description": "Navigation operation to perform",
                    "enum": [
                        "go_to_definition",
                        "find_references",
                        "hover",
                        "document_symbol",
                        "workspace_symbol",
                        "go_to_implementation",
                        "incoming_calls",
                        "outgoing_calls",
                    ],
                },
                "file": {
                    "type": "string",
                    "description": "Path to the source file",
                },
                "line": {
                    "type": "integer",
                    "description": "Line number (1-based)",
                },
                "character": {
                    "type": "integer",
                    "description": "Character position (0-based)",
                },
            },
            "required": ["operation", "file", "line", "character"],
        }

    async def execute(self, **kwargs: Any) -> str:
        return _NOT_IMPLEMENTED
