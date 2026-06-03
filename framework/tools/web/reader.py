"""WebReaderTool — fetch and read URL content."""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from framework.core.tool_manager import Tool, ToolConfig

logger = logging.getLogger(__name__)

_MAX_CONTENT_LENGTH = 50_000
_USER_AGENT = "Mozilla/5.0 (compatible; ModexAgent/0.1)"


class WebReaderTool(Tool):
    """Fetch content from a URL and convert to readable text.

    Supports HTML-to-markdown conversion via *markdownify*.
    Only reads static HTML — JavaScript-rendered pages may not return full content.
    """

    def __init__(self) -> None:
        super().__init__(
            name="web_reader",
            description=(
                "Fetch content from a URL and convert to readable text. "
                "Supports HTML-to-markdown conversion. "
                "Only reads static HTML; JavaScript-rendered pages may not return full content."
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
                    "timeout": {
                        "type": "integer",
                        "description": "Request timeout in seconds (default: 20).",
                        "default": 20,
                    },
                },
                "required": ["url"],
            },
            config=ToolConfig(timeout=60.0),
        )

    async def execute(self, url: str = "", format: str = "markdown", timeout: int = 20, **kwargs: Any) -> str:
        output_format = format

        # --- validate -----------------------------------------------------------
        if not url or not url.strip():
            return "Error: URL must not be empty."
        if not re.match(r"^https?://", url):
            return "Error: URL must start with http:// or https://"

        # --- lazy import markdownify --------------------------------------------
        try:
            from markdownify import markdownify as md
        except ImportError:
            return (
                "Error: markdownify package not installed. "
                "Install with: pip install markdownify"
            )

        # --- fetch --------------------------------------------------------------
        try:
            async with httpx.AsyncClient(
                follow_redirects=True,
                timeout=timeout,
                headers={"User-Agent": _USER_AGENT},
            ) as client:
                response = await client.get(url)
        except httpx.TimeoutException:
            return f"Error: Request timed out after {timeout}s for URL: {url}"
        except httpx.RequestError as exc:
            return f"Error fetching URL: {exc}"

        if response.status_code >= 400:
            return f"Error: HTTP {response.status_code} for URL: {url}"

        # --- content-type handling ----------------------------------------------
        content_type = response.headers.get("content-type", "").lower()

        if "text/html" in content_type:
            content = md(response.text, heading_style="ATX", bullets="-")
        elif content_type.startswith("text/"):
            content = response.text
        else:
            return (
                f"Error: Unsupported content type: {content_type}. "
                "web_reader supports text/html and text/* content."
            )

        # --- plain-text mode ----------------------------------------------------
        if output_format == "text":
            content = _strip_markdown(content)

        # --- truncate -----------------------------------------------------------
        if len(content) > _MAX_CONTENT_LENGTH:
            content = content[:_MAX_CONTENT_LENGTH]
            content += f"\n\n[... content truncated at {_MAX_CONTENT_LENGTH} characters ...]"

        return content


# -- helpers -----------------------------------------------------------------


def _strip_markdown(text: str) -> str:
    """Remove common markdown formatting to produce plain text."""
    # Headings
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    # Bold / italic
    text = re.sub(r"\*{1,3}([^*]+)\*{1,3}", r"\1", text)
    text = re.sub(r"_{1,3}([^_]+)_{1,3}", r"\1", text)
    # Images → alt text (must run before links)
    text = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", text)
    # Links → URL
    text = re.sub(r"\[([^\]]*)\]\(([^)]+)\)", r"\1 (\2)", text)
    # Inline code
    text = re.sub(r"`([^`]+)`", r"\1", text)
    return text
