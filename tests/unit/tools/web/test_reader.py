"""Tests for WebReaderTool stub."""

from __future__ import annotations

import pytest

from framework.tools.web.reader import WebReaderTool


class TestWebReaderTool:
    def test_name_is_web_reader(self) -> None:
        tool = WebReaderTool()
        assert tool.name == "web_reader"

    def test_description_is_not_empty(self) -> None:
        tool = WebReaderTool()
        assert tool.description

    def test_parameters_has_url_required(self) -> None:
        tool = WebReaderTool()
        assert "url" in tool.parameters["required"]

    @pytest.mark.asyncio
    async def test_execute_returns_not_implemented(self) -> None:
        tool = WebReaderTool()
        result = await tool.execute(url="https://example.com")
        assert "Not yet implemented" in result
        assert "https://example.com" in result
