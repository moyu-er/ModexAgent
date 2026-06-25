"""Web tools — search and page reader.

WebSearchTool provides DuckDuckGo-based web search.
WebReaderTool fetches URLs and converts HTML to markdown.
"""

from modex_agent.tools.web.reader import WebReaderTool
from modex_agent.tools.web.search import WebSearchTool

__all__ = ["WebSearchTool", "WebReaderTool"]
