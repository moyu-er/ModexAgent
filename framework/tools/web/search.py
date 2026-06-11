"""WebSearchTool — search the web via DuckDuckGo."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from framework.core.tool_manager import Tool, ToolConfig

logger = logging.getLogger(__name__)


class WebSearchTool(Tool):
    """Search the web using DuckDuckGo.

    Returns titles, URLs, and snippets for each result.
    Requires the ``ddgs`` package.
    """

    def __init__(self) -> None:
        super().__init__(
            name="web_search",
            description=(
                "Search the web using DuckDuckGo. "
                "Returns titles, URLs, and snippets for each result."
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
                        "description": "Maximum number of results (default 5, max 20).",
                        "default": 5,
                        "minimum": 1,
                        "maximum": 20,
                    },
                },
                "required": ["query"],
            },
            config=ToolConfig(),
        )

    async def execute(self, query: str = "", max_results: int = 5, **kwargs: Any) -> str:

        # --- validate -----------------------------------------------------------
        if not query or not query.strip():
            return "Error: Search query must not be empty."

        # --- lazy import --------------------------------------------------------
        try:
            from ddgs import DDGS
        except ImportError:
            return (
                "Error: ddgs package not installed. "
                "Install with: pip install ddgs"
            )

        # --- search (DDGS is synchronous → run in thread) -----------------------
        def _do_search() -> list[dict[str, str]]:
            with DDGS() as ddgs:
                return ddgs.text(query, max_results=max_results) or []

        try:
            results = await asyncio.to_thread(_do_search)
        except Exception as exc:
            logger.debug("WebSearchTool error: %s", exc, exc_info=True)
            return f"Error searching the web: {exc}"

        # --- format results -----------------------------------------------------
        if not results:
            return f"No results found for: {query}"

        lines = [f"Found {len(results)} results for \"{query}\":"]
        for i, r in enumerate(results, 1):
            lines.append(f"\n{i}. {r.get('title', 'No title')}")
            lines.append(f"   URL: {r.get('href', '')}")
            body = r.get("body", "")
            if body:
                lines.append(f"   {body}")
        return "\n".join(lines)
