"""Tests for WebReaderTool."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from modex_agent.tools.web.reader import WebReaderTool


def _mock_response(
    text: str = "<html><body><h1>Hello</h1><p>World</p></body></html>",
    status_code: int = 200,
    content_type: str = "text/html; charset=utf-8",
) -> MagicMock:
    """Build a mock httpx.Response."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.text = text
    resp.headers = {"content-type": content_type}
    return resp


class TestWebReaderToolSchema:
    def test_name_is_web_reader(self) -> None:
        tool = WebReaderTool()
        assert tool.name == "web_reader"

    def test_description_mentions_url(self) -> None:
        tool = WebReaderTool()
        assert tool.description
        assert "NOT YET IMPLEMENTED" not in tool.description

    def test_parameters_has_url_required(self) -> None:
        tool = WebReaderTool()
        assert "url" in tool.parameters["required"]

    def test_parameters_has_format_and_timeout(self) -> None:
        tool = WebReaderTool()
        props = tool.parameters["properties"]
        assert "format" in props
        assert props["format"]["default"] == "markdown"
        assert "timeout" in props
        assert props["timeout"]["default"] == 20


class TestWebReaderToolExecute:
    @pytest.mark.asyncio
    async def test_fetches_html_as_markdown(self) -> None:
        html = "<html><body><h1>Title</h1><p>Paragraph</p></body></html>"
        mock_resp = _mock_response(html)

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        tool = WebReaderTool()
        with patch("modex_agent.tools.web.reader.httpx.AsyncClient", return_value=mock_client):
            result = await tool.execute(url="https://example.com")

        assert "Title" in result
        assert "Paragraph" in result

    @pytest.mark.asyncio
    async def test_text_format_strips_markdown(self) -> None:
        html = "<html><body><h1>Heading</h1><p><strong>Bold</strong> text</p></body></html>"
        mock_resp = _mock_response(html)

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        tool = WebReaderTool()
        with patch("modex_agent.tools.web.reader.httpx.AsyncClient", return_value=mock_client):
            result = await tool.execute(url="https://example.com", format="text")

        # Should NOT contain markdown formatting markers like # or **
        # (markdownify may produce them but _strip_markdown should remove them)
        assert "##" not in result
        assert "**" not in result

    @pytest.mark.asyncio
    async def test_invalid_url_returns_error(self) -> None:
        tool = WebReaderTool()
        result = await tool.execute(url="not-a-url")
        assert "Error" in result
        assert "http://" in result or "https://" in result

    @pytest.mark.asyncio
    async def test_empty_url_returns_error(self) -> None:
        tool = WebReaderTool()
        result = await tool.execute(url="")
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_http_404_returns_error(self) -> None:
        mock_resp = _mock_response(status_code=404)

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        tool = WebReaderTool()
        with patch("modex_agent.tools.web.reader.httpx.AsyncClient", return_value=mock_client):
            result = await tool.execute(url="https://example.com/missing")

        assert "Error" in result
        assert "404" in result

    @pytest.mark.asyncio
    async def test_timeout_returns_error(self) -> None:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=httpx.TimeoutException("timed out"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        tool = WebReaderTool()
        with patch("modex_agent.tools.web.reader.httpx.AsyncClient", return_value=mock_client):
            result = await tool.execute(url="https://example.com", timeout=5)

        assert "Error" in result
        assert "timed out" in result.lower()

    @pytest.mark.asyncio
    async def test_binary_content_type_returns_error(self) -> None:
        mock_resp = _mock_response(content_type="image/png")

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        tool = WebReaderTool()
        with patch("modex_agent.tools.web.reader.httpx.AsyncClient", return_value=mock_client):
            result = await tool.execute(url="https://example.com/image.png")

        assert "Error" in result
        assert "Unsupported content type" in result

    @pytest.mark.asyncio
    async def test_text_plain_content_type(self) -> None:
        mock_resp = _mock_response(
            text="Just plain text content",
            content_type="text/plain; charset=utf-8",
        )

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        tool = WebReaderTool()
        with patch("modex_agent.tools.web.reader.httpx.AsyncClient", return_value=mock_client):
            result = await tool.execute(url="https://example.com/readme.txt")

        assert "Just plain text content" in result

    @pytest.mark.asyncio
    async def test_large_content_is_truncated(self) -> None:
        long_html = f"<html><body><p>{'x' * 60_000}</p></body></html>"
        mock_resp = _mock_response(long_html)

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        tool = WebReaderTool()
        with patch("modex_agent.tools.web.reader.httpx.AsyncClient", return_value=mock_client):
            result = await tool.execute(url="https://example.com/huge")

        assert "truncated" in result
        assert len(result) < 60_000

    @pytest.mark.asyncio
    async def test_request_error_returns_error(self) -> None:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=httpx.ConnectError("Connection refused"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        tool = WebReaderTool()
        with patch("modex_agent.tools.web.reader.httpx.AsyncClient", return_value=mock_client):
            result = await tool.execute(url="https://unreachable.example.com")

        assert "Error" in result

    @pytest.mark.asyncio
    async def test_markdownify_import_error_returns_install_hint(self) -> None:
        tool = WebReaderTool()
        with patch.dict("sys.modules", {"markdownify": None}):
            result = await tool.execute(url="https://example.com")

        assert "Error" in result
        assert "markdownify" in result
