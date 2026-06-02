"""WebSearchTool — search the web. Stub — not yet implemented."""

from __future__ import annotations

from typing import Any

from framework.core.tool_manager import Tool, ToolConfig


class WebSearchTool(Tool):
    """Search the web for information. NOT YET IMPLEMENTED.

    This is a placeholder stub. Use alternative approaches:
    - Check project documentation and codebase.
    - Ask the user for clarification.
    - Use existing MCP tools if configured.
    """

    def __init__(self) -> None:
        super().__init__(
            name="web_search",
            description=(
                "Search the web for information. NOT YET IMPLEMENTED — "
                "use alternative approaches (codebase search, user clarification)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query.",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of results (default 5).",
                        "default": 5,
                    },
                },
                "required": ["query"],
            },
            config=ToolConfig(),
        )

    async def execute(self, **kwargs: Any) -> str:
        query = kwargs.get("query", "")
        return (
            f"[web_search] Not yet implemented.\n\n"
            f"Query: {query}\n\n"
            f"Alternative approaches:\n"
            f"1. Search the codebase: use grep to find relevant files.\n"
            f"2. Check project docs in docs/ directory.\n"
            f"3. Ask the user for clarification.\n"
            f"4. Use MCP tools if configured for web access."
        )
