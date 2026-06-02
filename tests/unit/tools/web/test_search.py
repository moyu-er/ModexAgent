"""Tests for WebSearchTool stub."""

from __future__ import annotations

import pytest

from framework.tools.web.search import WebSearchTool


class TestWebSearchTool:
    def test_name_is_web_search(self) -> None:
        tool = WebSearchTool()
        assert tool.name == "web_search"

    def test_description_is_not_empty(self) -> None:
        tool = WebSearchTool()
        assert tool.description

    def test_parameters_has_query_required(self) -> None:
        tool = WebSearchTool()
        assert "query" in tool.parameters["required"]

    @pytest.mark.asyncio
    async def test_execute_returns_not_implemented(self) -> None:
        tool = WebSearchTool()
        result = await tool.execute(query="test query")
        assert "Not yet implemented" in result
        assert "test query" in result

    @pytest.mark.asyncio
    async def test_execute_without_query(self) -> None:
        tool = WebSearchTool()
        result = await tool.execute()
        assert "Not yet implemented" in result
