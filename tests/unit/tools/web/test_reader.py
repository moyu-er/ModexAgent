"""Tests for WebReaderTool on the guarded HTTP stack.

Network behavior is exercised through the real httpx + httpcore +
ValidatingNetworkBackend stack with only the socket scripted
(conftest.ScriptedBackend) — no client mocking.
"""

from __future__ import annotations

import sys

import anyio
import httpcore
import pytest

from modex_agent.tools.web.reader import WebReaderTool
from tests.unit.tools.web.conftest import (
    ScriptedBackend,
    fake_resolver,
    html_response,
    redirect_response,
    text_response,
)

PUBLIC_A = "93.184.216.34"
PUBLIC_B = "1.1.1.1"

Answers = dict[str, list[str]]
Responses = dict[str, list[httpcore.Response]]


def make_tool(answers: Answers, responses: Responses) -> tuple[ScriptedBackend, WebReaderTool]:
    scripted = ScriptedBackend(responses)
    tool = WebReaderTool(resolver=fake_resolver(answers), dialer=scripted)
    return scripted, tool


async def test_plain_text_keeps_separate_underscore_spans() -> None:
    _backend, tool = make_tool(
        {"example.com": [PUBLIC_A]},
        {PUBLIC_A: [text_response("_first_ and _second_")]},
    )

    result = await tool.execute(url="http://example.com", format="text")

    assert result == "first and second"


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
    async def test_fetches_html_as_markdown(self) -> None:
        scripted, tool = make_tool(
            {"example.com": [PUBLIC_A]},
            {PUBLIC_A: [html_response("<html><body><h1>Title</h1><p>Paragraph</p></body></html>")]},
        )
        result = await tool.execute(url="http://example.com")
        assert "Title" in result
        assert "Paragraph" in result
        assert scripted.dials == [(PUBLIC_A, 80)]

    async def test_text_format_strips_markdown(self) -> None:
        _scripted, tool = make_tool(
            {"example.com": [PUBLIC_A]},
            {
                PUBLIC_A: [
                    html_response(
                        "<html><body><h1>Heading</h1><p><strong>Bold</strong> text</p></body></html>"
                    )
                ]
            },
        )
        result = await tool.execute(url="http://example.com", format="text")
        assert "##" not in result
        assert "**" not in result

    async def test_invalid_url_returns_error(self) -> None:
        tool = WebReaderTool()
        result = await tool.execute(url="not-a-url")
        assert "Error" in result
        assert "http://" in result or "https://" in result

    async def test_empty_url_returns_error(self) -> None:
        tool = WebReaderTool()
        result = await tool.execute(url="")
        assert "Error" in result

    async def test_http_404_returns_error(self) -> None:
        _scripted, tool = make_tool(
            {"example.com": [PUBLIC_A]},
            {PUBLIC_A: [httpcore.Response(404, content=b"missing")]},
        )
        result = await tool.execute(url="http://example.com/missing")
        assert "Error" in result
        assert "404" in result

    async def test_timeout_returns_error(self) -> None:
        class SleepingBackend(httpcore.AsyncNetworkBackend):
            async def connect_tcp(
                self,
                host: str,
                port: int,
                timeout: float | None = None,
                local_address: str | None = None,
                socket_options: object = None,
            ) -> httpcore.AsyncNetworkStream:
                # Real backends enforce the connect timeout and map the
                # raw TimeoutError to httpcore.ConnectTimeout.
                try:
                    with anyio.fail_after(timeout):
                        await anyio.sleep(10)
                except TimeoutError:
                    raise httpcore.ConnectTimeout("connect timed out") from None
                raise httpcore.ConnectTimeout("unreachable")

        tool = WebReaderTool(
            resolver=fake_resolver({"slow.example": [PUBLIC_A]}), dialer=SleepingBackend()
        )
        result = await tool.execute(url="http://slow.example", timeout=1)
        assert "Error" in result
        assert "timed out" in result.lower() or "timeout" in result.lower()

    async def test_binary_content_type_returns_error(self) -> None:
        _scripted, tool = make_tool(
            {"example.com": [PUBLIC_A]},
            {
                PUBLIC_A: [
                    httpcore.Response(
                        200,
                        headers=[(b"content-type", b"image/png")],
                        content=b"\x89PNG",
                    )
                ]
            },
        )
        result = await tool.execute(url="http://example.com/image.png")
        assert "Error" in result
        assert "Unsupported content type" in result

    async def test_text_plain_content_type(self) -> None:
        _scripted, tool = make_tool(
            {"example.com": [PUBLIC_A]},
            {PUBLIC_A: [text_response("Just plain text content")]},
        )
        result = await tool.execute(url="http://example.com/readme.txt")
        assert "Just plain text content" in result

    async def test_large_content_is_truncated(self) -> None:
        _scripted, tool = make_tool(
            {"example.com": [PUBLIC_A]},
            {PUBLIC_A: [html_response(f"<html><body><p>{'x' * 60_000}</p></body></html>")]},
        )
        result = await tool.execute(url="http://example.com/huge")
        assert "truncated" in result
        assert len(result) < 60_000

    async def test_request_error_returns_error(self) -> None:
        _scripted, tool = make_tool(
            {"unreachable.example": [PUBLIC_A]},
            {},  # no scripted response → ConnectError at dial
        )
        result = await tool.execute(url="http://unreachable.example")
        assert "Error" in result

    async def test_markdownify_import_error_returns_install_hint(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tool = WebReaderTool()
        monkeypatch.setitem(sys.modules, "markdownify", None)
        result = await tool.execute(url="http://example.com")
        assert "Error" in result
        assert "markdownify" in result


class TestWebReaderToolSSRF:
    """The tool itself is the execution-time SSRF gate."""

    async def test_private_dns_name_blocked_with_actionable_error(self) -> None:
        scripted, tool = make_tool({"intranet.corp": ["10.0.0.9"]}, {"10.0.0.9": [html_response()]})
        result = await tool.execute(url="http://intranet.corp/wiki")
        assert "Error" in result
        assert "SSRF" in result or "private" in result
        assert scripted.dials == []

    async def test_literal_private_url_blocked_before_any_dial(self) -> None:
        scripted, tool = make_tool({}, {})
        result = await tool.execute(url="http://192.168.1.1/admin")
        assert "Error" in result
        assert "192.168.1.1" in result
        assert scripted.dials == []

    async def test_redirect_to_metadata_blocked(self) -> None:
        scripted, tool = make_tool(
            {"a.example": [PUBLIC_A]},
            {PUBLIC_A: [redirect_response("http://169.254.169.254/latest/meta-data")]},
        )
        result = await tool.execute(url="http://a.example/")
        assert "Error" in result
        assert "169.254.169.254" in result
        assert scripted.dials == [(PUBLIC_A, 80)]

    async def test_redirect_to_loopback_blocked(self) -> None:
        scripted, tool = make_tool(
            {"a.example": [PUBLIC_A]},
            {PUBLIC_A: [redirect_response("http://127.0.0.1:8080/secret")]},
        )
        result = await tool.execute(url="http://a.example/")
        assert "Error" in result
        assert "127.0.0.1" in result
        assert scripted.dials == [(PUBLIC_A, 80)]

    async def test_redirect_chain_public_ok(self) -> None:
        scripted, tool = make_tool(
            {"a.example": [PUBLIC_A], "b.example": [PUBLIC_B]},
            {
                PUBLIC_A: [redirect_response("http://b.example/page")],
                PUBLIC_B: [html_response("<html><body><h1>Final</h1></body></html>")],
            },
        )
        result = await tool.execute(url="http://a.example/")
        assert "Final" in result
        assert scripted.dials == [(PUBLIC_A, 80), (PUBLIC_B, 80)]

    async def test_excessive_redirects_error(self) -> None:
        responses: Responses = {
            PUBLIC_A: [redirect_response("http://a.example/loop") for _ in range(10)],
        }
        scripted, tool = make_tool({"a.example": [PUBLIC_A]}, responses)
        result = await tool.execute(url="http://a.example/")
        assert "Error" in result
        assert "redirect" in result.lower()
        assert len(scripted.dials) <= 6  # initial + 5 allowed hops; hop 6 refused pre-dial

    async def test_env_proxy_cannot_bypass(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:7890")
        monkeypatch.setenv("http_proxy", "http://127.0.0.1:7890")
        monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:7890")
        scripted, tool = make_tool(
            {"example.com": [PUBLIC_A]},
            {PUBLIC_A: [html_response()]},
        )
        result = await tool.execute(url="http://example.com/")
        assert "Error" not in result
        assert scripted.dials == [(PUBLIC_A, 80)]
