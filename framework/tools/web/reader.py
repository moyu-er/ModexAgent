"""WebReaderTool — fetch and read URL content. Stub — not yet implemented."""

from __future__ import annotations

from typing import Any

from framework.core.tool_manager import Tool, ToolConfig


class WebReaderTool(Tool):
    """Fetch and read content from a URL. NOT YET IMPLEMENTED.

    This is a placeholder stub. Full implementation will support:
    - HTML → markdown conversion via markdownify
    - Configurable timeout
    - Response caching
    """

    def __init__(self) -> None:
        super().__init__(
            name="web_reader",
            description=(
                "Fetch and read content from a URL. NOT YET IMPLEMENTED."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "URL to fetch and read.",
                    },
                    "format": {
                        "type": "string",
                        "enum": ["markdown", "text"],
                        "description": "Output format (default: markdown).",
                        "default": "markdown",
                    },
                },
                "required": ["url"],
            },
            config=ToolConfig(),
        )

    async def execute(self, **kwargs: Any) -> str:
        url = kwargs.get("url", "")
        return (
            f"[web_reader] Not yet implemented.\n\n"
            f"URL: {url}\n\n"
            f"Use alternative approaches or ask the user for the content."
        )
