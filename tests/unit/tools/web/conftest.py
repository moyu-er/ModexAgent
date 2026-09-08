"""Shared wire-level fakes for the guarded-HTTP tests.

Everything here rides the real httpx + httpcore + guarded-backend stack
with only the socket replaced: DNS answers are injected per hostname,
and each dial hands out an httpcore response scripted for that origin.
Scripted responses close every connection (``connection: close``) so
each hop is one fresh scripted response.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Callable, Mapping

import httpcore

from modex_agent.tools.web.guarded_transport import AsyncResolver

ResolverAnswers = Mapping[str, list[str]]
DialSink = Callable[[str, int], None]


def _response_bytes(response: httpcore.Response) -> list[bytes]:
    response.read()
    assert response.content is not None
    header_lines: list[bytes] = []
    for key, value in response.headers:
        if key == b"content-length":
            continue
        header_lines.append(key + b": " + value + b"\r\n")
    header_lines.append(b"content-length: %d\r\n" % len(response.content))
    header_lines.append(b"connection: close\r\n")
    return [
        f"HTTP/1.1 {response.status} Reason\r\n".encode("ascii"),
        *header_lines,
        b"\r\n",
        response.content,
    ]


class ScriptedBackend(httpcore.AsyncNetworkBackend):
    """Answers dials from a per-origin scripted httpcore response list.

    Records every (host, port) dialed so tests can assert the guarded
    stack dialed the validated IP literal, never a raw hostname.
    """

    def __init__(
        self,
        responses: Mapping[str, list[httpcore.Response]],
        dial_sink: DialSink | None = None,
    ) -> None:
        self._responses = responses
        self._dial_sink = dial_sink
        self.dials: list[tuple[str, int]] = []

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: object = None,
    ) -> httpcore.AsyncNetworkStream:
        self.dials.append((host, port))
        if self._dial_sink is not None:
            self._dial_sink(host, port)
        script = self._responses.get(host)
        if not script:
            raise httpcore.ConnectError(f"no scripted response for {host}")
        response = script.pop(0)
        return httpcore.AsyncMockStream(_response_bytes(response))

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: object = None,
    ) -> httpcore.AsyncNetworkStream:  # pragma: no cover - guarded stack never dials UDS
        raise httpcore.ConnectError("unix sockets unsupported")


def fake_resolver(answers: ResolverAnswers) -> AsyncResolver:
    """An ``AsyncResolver`` answering from the table, no real DNS."""

    async def resolve(host: str, port: int) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
        names = answers.get(host)
        if names is None:
            raise OSError(f"no DNS answer for {host}")
        return [ipaddress.ip_address(n) for n in names]

    return resolve


def html_response(body: str = "<html><body><h1>Title</h1></body></html>") -> httpcore.Response:
    return httpcore.Response(
        200,
        headers=[(b"content-type", b"text/html; charset=utf-8")],
        content=body.encode("utf-8"),
    )


def redirect_response(location: str, status: int = 302) -> httpcore.Response:
    return httpcore.Response(
        status,
        headers=[
            (b"location", location.encode("ascii")),
            (b"content-length", b"0"),
        ],
    )


def text_response(body: str) -> httpcore.Response:
    return httpcore.Response(
        200,
        headers=[(b"content-type", b"text/plain; charset=utf-8")],
        content=body.encode("utf-8"),
    )
