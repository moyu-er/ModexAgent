"""Guarded transport adaptation — resolver, validating backend, httpcore adapter.

Transport half of the guarded HTTP stack; the client/redirect/fetch
orchestration lives in ``guarded_http``. Every dial resolves the
hostname, validates EVERY resolved address against the NetworkGuard
tables (fail closed on any forbidden answer), then dials the validated
IP *literal* — the underlying client cannot re-resolve, closing the
DNS-rebinding TOCTOU at connect time. TLS SNI and certificate
verification stay with the httpcore pool (it wraps the raw stream with
the ORIGIN hostname), so dialing an IP cannot weaken HTTPS identity.
"""

from __future__ import annotations

import ipaddress
from collections.abc import (
    AsyncIterable,
    AsyncIterator,
    Awaitable,
    Callable,
    Iterable,
    Mapping,
)

import anyio
import httpcore
import httpx

from modex_agent.sandbox.guard_network import NetworkGuard

__all__ = [
    "AsyncResolver",
    "GuardedAsyncTransport",
    "ValidatingNetworkBackend",
]

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
AddressList = list[IPAddress]
AsyncResolver = Callable[[str, int], Awaitable[AddressList]]


def _default_dialer() -> httpcore.AsyncNetworkBackend:
    # ``httpcore.AnyIOBackend`` is a conditional third-party export: the
    # package __init__ substitutes a construction-raising stub when anyio
    # is absent. anyio is a hard dependency here, so the export is real —
    # narrow it at this extension boundary so the declared type is earned.
    backend = httpcore.AnyIOBackend()
    if not isinstance(backend, httpcore.AsyncNetworkBackend):
        raise RuntimeError("httpcore.AnyIOBackend is not an AsyncNetworkBackend")
    return backend


async def _anyio_resolver(host: str, port: int) -> list[IPAddress]:
    infos = await anyio.getaddrinfo(host, port)
    return [ipaddress.ip_address(info[4][0]) for info in infos]


class ValidatingNetworkBackend(httpcore.AsyncNetworkBackend):
    """Resolve → validate every answer → dial a validated IP literal.

    ``dialer`` opens the actual streams (default: httpcore's public
    AnyIO backend — an IP literal is passed straight to ``connect_tcp``
    without a second resolution). ``resolver`` is the DNS seam
    (injectable for tests).
    """

    def __init__(
        self,
        *,
        guard: NetworkGuard | None = None,
        resolver: AsyncResolver | None = None,
        dialer: httpcore.AsyncNetworkBackend | None = None,
    ) -> None:
        self._guard = guard if guard is not None else NetworkGuard()
        self._resolver = resolver if resolver is not None else _anyio_resolver
        self._dialer = dialer if dialer is not None else _default_dialer()

    @property
    def guard(self) -> NetworkGuard:
        return self._guard

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        target = await self._validated_target(host, port)
        return await self._dialer.connect_tcp(
            host=str(target),
            port=port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        raise httpcore.ConnectError("unix domain sockets are not permitted")

    async def sleep(self, seconds: float) -> None:
        await anyio.sleep(seconds)

    async def _validated_target(self, host: str, port: int) -> IPAddress:
        try:
            addr = ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            return self._require_allowed(addr, host)
        try:
            answers = await self._resolver(host, port)
        except OSError as exc:
            raise httpcore.ConnectError(
                f"SSRF guard: DNS resolution failed for {host}: {exc}"
            ) from exc
        if not answers:
            raise httpcore.ConnectError(f"SSRF guard: no DNS answer for {host}")
        return self._require_allowed(answers[0], host, answers[1:])

    def _require_allowed(
        self, addr: IPAddress, host: str, rest: Iterable[IPAddress] = ()
    ) -> IPAddress:
        decision = self._guard.evaluate_address(str(addr))
        if not decision.allowed:
            raise httpcore.ConnectError(f"SSRF blocked {host} -> {addr}: {decision.reason}")
        for other in rest:
            decision = self._guard.evaluate_address(str(other))
            if not decision.allowed:
                raise httpcore.ConnectError(f"SSRF blocked {host} -> {other}: {decision.reason}")
        return addr


# Most-specific first; first isinstance hit wins the mapping.
_HTTPCORE_TO_HTTPX: Mapping[type[Exception], type[httpx.HTTPError]] = {
    httpcore.ConnectTimeout: httpx.ConnectTimeout,
    httpcore.ReadTimeout: httpx.ReadTimeout,
    httpcore.WriteTimeout: httpx.WriteTimeout,
    httpcore.PoolTimeout: httpx.PoolTimeout,
    httpcore.TimeoutException: httpx.TimeoutException,
    httpcore.LocalProtocolError: httpx.LocalProtocolError,
    httpcore.RemoteProtocolError: httpx.RemoteProtocolError,
    httpcore.ProtocolError: httpx.ProtocolError,
    httpcore.ConnectError: httpx.ConnectError,
    httpcore.ReadError: httpx.ReadError,
    httpcore.WriteError: httpx.WriteError,
    httpcore.NetworkError: httpx.NetworkError,
    httpcore.ProxyError: httpx.ProxyError,
    httpcore.UnsupportedProtocol: httpx.UnsupportedProtocol,
}


def _raise_mapped(exc: Exception) -> None:
    """Map expected httpcore errors to their httpx twins; re-raise unknowns."""
    for from_exc, to_exc in _HTTPCORE_TO_HTTPX.items():
        if isinstance(exc, from_exc):
            raise to_exc(str(exc)) from exc
    raise exc


class _AsyncResponseStream(httpx.AsyncByteStream):
    """httpx view of an httpcore response body.

    ``body`` is the async iterable half of httpcore's sync/async stream
    union; ``close`` is the response's public ``aclose`` hook, injected
    so the stream never reaches back into httpcore response internals.
    """

    def __init__(
        self,
        body: AsyncIterable[bytes],
        close: Callable[[], Awaitable[None]],
    ) -> None:
        self._body = body
        self._close = close

    async def __aiter__(self) -> AsyncIterator[bytes]:
        try:
            async for chunk in self._body:
                yield chunk
        except Exception as exc:
            _raise_mapped(exc)

    async def aclose(self) -> None:
        await self._close()


class GuardedAsyncTransport(httpx.AsyncBaseTransport):
    """httpx transport over a public httpcore pool with a guarded backend."""

    def __init__(self, backend: ValidatingNetworkBackend) -> None:
        self._pool = httpcore.AsyncConnectionPool(network_backend=backend)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        req = httpcore.Request(
            method=request.method,
            url=httpcore.URL(
                scheme=request.url.raw_scheme,
                host=request.url.raw_host,
                port=request.url.port,
                target=request.url.raw_path,
            ),
            headers=request.headers.raw,
            content=request.stream,
            extensions=request.extensions,
        )
        try:
            resp = await self._pool.handle_async_request(req)
        except Exception as exc:
            _raise_mapped(exc)
            raise  # pragma: no cover - _raise_mapped always raises
        # httpcore annotates the response body as a sync/async union; the
        # async pool always hands back the async shape. Narrow it at this
        # third-party seam (the same idiom httpx's own transport uses).
        body = resp.stream
        assert isinstance(body, AsyncIterable)
        return httpx.Response(
            status_code=resp.status,
            headers=resp.headers,
            stream=_AsyncResponseStream(body, resp.aclose),
            extensions=resp.extensions,
        )

    async def aclose(self) -> None:
        await self._pool.aclose()
