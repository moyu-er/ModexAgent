"""lsp_diagnostics stub — get LSP errors/warnings for a file or directory."""

from __future__ import annotations

from typing import Any

from framework.core.tool_manager import Tool

_NOT_IMPLEMENTED = (
    "LSP diagnostics is not yet implemented. This tool will be available in a future update."
)


class LspDiagnosticsTool(Tool):
    """Get language server diagnostics for a file or directory (stub)."""

    def __init__(self) -> None:
        super().__init__()

    @property
    def name(self) -> str:
        return "lsp_diagnostics"

    @property
    def description(self) -> str:
        return (
            "Get errors, warnings, and hints from language servers for a file or directory. "
            "Use BEFORE running builds to catch issues early. "
            f"(Note: {_NOT_IMPLEMENTED})"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "file": {
                    "type": "string",
                    "description": "Path to a source file or directory to check",
                },
            },
            "required": ["file"],
        }

    async def execute(self, file: str, **kwargs: object) -> str:
        return _NOT_IMPLEMENTED
