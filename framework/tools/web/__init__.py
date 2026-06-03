"""Web tools — search and page reader.

WebSearchTool provides DuckDuckGo-based web search.
WebReaderTool fetches URLs and converts HTML to markdown.
"""

from framework.tools.web.reader import WebReaderTool
from framework.tools.web.search import WebSearchTool

__all__ = ["WebSearchTool", "WebReaderTool"]
