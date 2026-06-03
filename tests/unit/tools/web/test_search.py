"""Tests for WebSearchTool."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

from framework.tools.web.search import WebSearchTool


def _mock_ddgs(results: list[dict[str, str]] | None = None, *, error: Exception | None = None):
    """Build a mock ddgs module with DDGS class.

    Returns (mock_module, mock_text_method) so tests can assert on call args.
    """
    mock_text = MagicMock(
        return_value=results if results is not None else [],
        side_effect=error,
    )
    mock_instance = MagicMock()
    mock_instance.__enter__ = MagicMock(return_value=mock_instance)
    mock_instance.__exit__ = MagicMock(return_value=False)
    if error is None:
        mock_instance.text = mock_text
    else:
        mock_instance.text = mock_text  # side_effect raises

    mock_ddgs_class = MagicMock(return_value=mock_instance)
    mock_module = MagicMock(DDGS=mock_ddgs_class)
    return mock_module, mock_text


class TestWebSearchToolSchema:
    def test_name_is_web_search(self) -> None:
        tool = WebSearchTool()
        assert tool.name == "web_search"

    def test_description_mentions_search(self) -> None:
        tool = WebSearchTool()
        assert tool.description
        assert "NOT YET IMPLEMENTED" not in tool.description

    def test_parameters_has_query_required(self) -> None:
        tool = WebSearchTool()
        assert "query" in tool.parameters["required"]

    def test_parameters_max_results_default(self) -> None:
        tool = WebSearchTool()
        assert tool.parameters["properties"]["max_results"]["default"] == 5


class TestWebSearchToolExecute:
    @pytest.mark.asyncio
    async def test_returns_formatted_results(self) -> None:
        results = [
            {"title": "Python", "href": "https://python.org", "body": "Welcome to Python."},
            {"title": "PyPI", "href": "https://pypi.org", "body": "Find packages."},
        ]
        mock_mod, _ = _mock_ddgs(results)

        tool = WebSearchTool()
        with patch.dict(sys.modules, {"ddgs": mock_mod}):
            result = await tool.execute(query="python")

        assert "Found 2 results" in result
        assert "Python" in result
        assert "https://python.org" in result
        assert "Welcome to Python." in result
        assert "PyPI" in result

    @pytest.mark.asyncio
    async def test_empty_query_returns_error(self) -> None:
        tool = WebSearchTool()
        result = await tool.execute(query="")
        assert "Error" in result
        assert "empty" in result.lower()

    @pytest.mark.asyncio
    async def test_whitespace_query_returns_error(self) -> None:
        tool = WebSearchTool()
        result = await tool.execute(query="   ")
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_no_results(self) -> None:
        mock_mod, _ = _mock_ddgs([])

        tool = WebSearchTool()
        with patch.dict(sys.modules, {"ddgs": mock_mod}):
            result = await tool.execute(query="obscurequery12345")

        assert "No results found" in result

    @pytest.mark.asyncio
    async def test_network_error_returns_error_string(self) -> None:
        mock_mod, _ = _mock_ddgs(error=RuntimeError("Connection failed"))

        tool = WebSearchTool()
        with patch.dict(sys.modules, {"ddgs": mock_mod}):
            result = await tool.execute(query="test")

        assert "Error" in result
        assert "Connection failed" in result

    @pytest.mark.asyncio
    async def test_max_results_passed_through(self) -> None:
        results = [
            {"title": f"Result {i}", "href": f"https://example.com/{i}", "body": f"Body {i}"}
            for i in range(3)
        ]
        mock_mod, mock_text = _mock_ddgs(results)

        tool = WebSearchTool()
        with patch.dict(sys.modules, {"ddgs": mock_mod}):
            await tool.execute(query="test", max_results=3)

        mock_text.assert_called_once_with("test", max_results=3)

    @pytest.mark.asyncio
    async def test_import_error_returns_install_hint(self) -> None:
        tool = WebSearchTool()
        # Block ddgs import to simulate missing package
        with patch.dict(sys.modules, {"ddgs": None}):
            result = await tool.execute(query="test")

        assert "Error" in result
        assert "ddgs" in result
