"""Execution-time SSRF protection tests — the guarded HTTP stack.

The stack under test: httpx AsyncClient (manual redirects, trust_env
off) → GuardedAsyncTransport → public httpcore AsyncConnectionPool →
ValidatingNetworkBackend (resolve → validate every answer → dial the
validated IP literal). Only the socket layer is scripted.
"""

from __future__ import annotations

import anyio
import httpcore
import httpx
import pytest

from modex_agent.sandbox.guard_network import NetworkGuard
from modex_agent.tools.web.guarded_http import (
    GuardedHttpError,
    PolicyBlockedError,
    follow_guarded,
    guarded_async_client,
)
from modex_agent.tools.web.guarded_transport import ValidatingNetworkBackend
from tests.unit.tools.web.conftest import (
    ScriptedBackend,
    fake_resolver,
    html_response,
    redirect_response,
)

PUBLIC_A = "93.184.216.34"
PUBLIC_B = "1.1.1.1"

Answers = dict[str, list[str]]
Responses = dict[str, list[httpcore.Response]]


def make_stack(
    answers: Answers,
    responses: Responses,
    *,
    guard: NetworkGuard | None = None,
) -> tuple[ScriptedBackend, httpx.AsyncClient]:
    scripted = ScriptedBackend(responses)
    backend = ValidatingNetworkBackend(
        guard=guard or NetworkGuard(),
        resolver=fake_resolver(answers),
        dialer=scripted,
    )
    return scripted, guarded_async_client(backend)


class TestValidatingBackendDials:
    """Resolution → validation → IP-literal dial, at connect time."""

    async def test_public_hostname_resolved_and_dialed_by_ip(self) -> None:
        scripted, client = make_stack(
            {"example.com": [PUBLIC_A]},
            {PUBLIC_A: [html_response()]},
        )
        async with client:
            response = await client.get("http://example.com/")
        assert response.status_code == 200
        assert scripted.dials == [(PUBLIC_A, 80)]

    async def test_literal_ip_url_skips_dns_dials_directly(self) -> None:
        scripted, client = make_stack(
            {},
            {PUBLIC_A: [html_response()]},
        )
        async with client:
            response = await client.get(f"http://{PUBLIC_A}/")
        assert response.status_code == 200
        assert scripted.dials == [(PUBLIC_A, 80)]

    async def test_port_honored(self) -> None:
        scripted, client = make_stack(
            {"example.com": [PUBLIC_A]},
            {PUBLIC_A: [html_response()]},
        )
        async with client:
            await client.get("http://example.com:8080/")
        assert scripted.dials == [(PUBLIC_A, 8080)]


class TestFailClosed:
    """Any forbidden answer blocks the whole dial — no fallback to a
    cleaner-looking sibling address (fail closed, not first-match)."""

    async def test_private_dns_answer_blocks(self) -> None:
        scripted, client = make_stack(
            {"internal.example": ["10.0.0.7"]},
            {"10.0.0.7": [html_response()]},
        )
        async with client:
            with pytest.raises(httpx.ConnectError) as excinfo:
                await client.get("http://internal.example/")
        assert "SSRF" in str(excinfo.value)
        assert scripted.dials == []

    async def test_mixed_public_and_private_answers_block(self) -> None:
        scripted, client = make_stack(
            {"split.example": [PUBLIC_A, "192.168.0.20"]},
            {PUBLIC_A: [html_response()]},
        )
        async with client:
            with pytest.raises(httpx.ConnectError) as excinfo:
                await client.get("http://split.example/")
        assert "192.168.0.20" in str(excinfo.value)
        assert scripted.dials == []

    async def test_dns_rebinding_second_answer_blocked(self) -> None:
        """Rebinding shape: TTL-stale public answer + fresh loopback answer."""
        scripted, client = make_stack(
            {"rebind.example": ["127.0.0.1", PUBLIC_A]},
            {PUBLIC_A: [html_response()]},
        )
        async with client:
            with pytest.raises(httpx.ConnectError):
                await client.get("http://rebind.example/")
        assert scripted.dials == []

    async def test_ipv6_loopback_answer_blocks(self) -> None:
        scripted, client = make_stack({"v6.example": ["::1"]}, {})
        async with client:
            with pytest.raises(httpx.ConnectError):
                await client.get("http://v6.example/")
        assert scripted.dials == []

    async def test_no_dns_answer_fails_closed(self) -> None:
        scripted, client = make_stack({}, {})
        async with client:
            with pytest.raises(httpx.ConnectError):
                await client.get("http://nxdomain.example/")
        assert scripted.dials == []

    async def test_allowed_network_config_permits_private(self) -> None:
        from modex_agent.sandbox.guard_network import NetworkGuardConfig

        guard = NetworkGuard(NetworkGuardConfig(allowed_networks=("10.0.0.0/8",)))
        scripted, client = make_stack(
            {"ok.example": ["10.0.0.7"]},
            {"10.0.0.7": [html_response()]},
            guard=guard,
        )
        async with client:
            response = await client.get("http://ok.example/")
        assert response.status_code == 200
        assert scripted.dials == [("10.0.0.7", 80)]


class TestManualRedirects:
    """Every hop is validated before sending; redirects stay manual."""

    async def test_redirect_to_public_host_followed(self) -> None:
        scripted, client = make_stack(
            {"a.example": [PUBLIC_A], "b.example": [PUBLIC_B]},
            {
                PUBLIC_A: [redirect_response("http://b.example/final")],
                PUBLIC_B: [html_response("<html><body><h1>Moved</h1></body></html>")],
            },
        )
        async with client:
            response = await follow_guarded(
                client, client.build_request("GET", "http://a.example/"), max_hops=3
            )
        assert response.status_code == 200
        assert "Moved" in response.text
        assert scripted.dials == [(PUBLIC_A, 80), (PUBLIC_B, 80)]

    async def test_redirect_to_metadata_ip_blocked(self) -> None:
        scripted, client = make_stack(
            {"a.example": [PUBLIC_A]},
            {PUBLIC_A: [redirect_response("http://169.254.169.254/latest")]},
        )
        async with client:
            with pytest.raises(PolicyBlockedError) as excinfo:
                await follow_guarded(
                    client, client.build_request("GET", "http://a.example/"), max_hops=3
                )
        assert "169.254.169.254" in str(excinfo.value)
        assert scripted.dials == [(PUBLIC_A, 80)]

    async def test_redirect_to_loopback_hostname_blocked_at_dial(self) -> None:
        """``localhost`` is a name — statically clean, blocked at resolve."""
        scripted, client = make_stack(
            {"a.example": [PUBLIC_A], "localhost": ["127.0.0.1"]},
            {
                PUBLIC_A: [redirect_response("http://localhost/x")],
            },
        )
        async with client:
            with pytest.raises((PolicyBlockedError, httpx.ConnectError)) as excinfo:
                await follow_guarded(
                    client, client.build_request("GET", "http://a.example/"), max_hops=3
                )
        assert "127.0.0.1" in str(excinfo.value) or "localhost" in str(excinfo.value)
        assert scripted.dials == [(PUBLIC_A, 80)]

    async def test_redirect_hop_limit_enforced(self) -> None:
        answers: Answers = {}
        responses: Responses = {}
        for i in range(5):
            host = f"hop{i}.example"
            addr = PUBLIC_A if i % 2 == 0 else PUBLIC_B
            answers[host] = [addr]
            responses.setdefault(addr, []).append(
                redirect_response(f"http://hop{i + 1}.example/next")
            )
        scripted, client = make_stack(answers, responses)
        async with client:
            with pytest.raises(GuardedHttpError) as excinfo:
                await follow_guarded(
                    client,
                    client.build_request("GET", "http://hop0.example/"),
                    max_hops=3,
                )
        assert "redirect" in str(excinfo.value).lower()
        assert len(scripted.dials) == 4  # initial + 3 allowed hops; hop 4 refused pre-dial

    async def test_redirect_cross_origin_strips_authorization(self) -> None:
        """httpx's ``next_request`` header hygiene is preserved: the
        cross-origin hop carries no Authorization header."""
        captured: list[httpx.Request] = []

        class CaptureTransport(httpx.AsyncBaseTransport):
            def __init__(self) -> None:
                self._n = 0

            async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
                captured.append(request)
                self._n += 1
                if self._n == 1:
                    return httpx.Response(302, headers={"location": "http://b.example/final"})
                return httpx.Response(200, text="ok")

        async with httpx.AsyncClient(transport=CaptureTransport()) as client:
            request = client.build_request(
                "GET", "http://a.example/", headers={"Authorization": "Bearer secret"}
            )
            response = await follow_guarded(client, request, max_hops=2)

        assert response.status_code == 200
        assert len(captured) == 2
        assert captured[0].headers["Authorization"] == "Bearer secret"
        assert "Authorization" not in captured[1].headers


class TestProxyAndEnv:
    """Proxies and environment mounts cannot bypass the guarded transport."""

    def test_client_factory_rejects_proxy_argument(self) -> None:
        backend = ValidatingNetworkBackend(
            guard=NetworkGuard(), resolver=fake_resolver({}), dialer=ScriptedBackend({})
        )
        with pytest.raises(TypeError):
            guarded_async_client(backend, proxy="http://127.0.0.1:7890")  # type: ignore[call-arg]

    def test_client_factory_rejects_mounts_argument(self) -> None:
        backend = ValidatingNetworkBackend(
            guard=NetworkGuard(), resolver=fake_resolver({}), dialer=ScriptedBackend({})
        )
        with pytest.raises(TypeError):
            guarded_async_client(
                backend,
                mounts={"all://": httpx.AsyncHTTPTransport()},  # type: ignore[call-arg]
            )

    async def test_env_proxies_ignored_even_when_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:7890")
        monkeypatch.setenv("http_proxy", "http://127.0.0.1:7890")
        monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:7890")
        scripted, client = make_stack(
            {"example.com": [PUBLIC_A]},
            {PUBLIC_A: [html_response()]},
        )
        async with client:
            response = await client.get("http://example.com/")
        assert response.status_code == 200
        assert scripted.dials == [(PUBLIC_A, 80)]


class TestTimeoutAndCleanup:
    async def test_connect_timeout_maps_to_httpx_timeout(self) -> None:
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

        backend = ValidatingNetworkBackend(
            guard=NetworkGuard(),
            resolver=fake_resolver({"slow.example": [PUBLIC_A]}),
            dialer=SleepingBackend(),
        )
        client = guarded_async_client(backend)
        async with client:
            with pytest.raises(httpx.TimeoutException):
                await client.get("http://slow.example/", timeout=0.2)

    async def test_pool_closed_after_client_exit(self) -> None:
        scripted, client = make_stack(
            {"example.com": [PUBLIC_A]},
            {PUBLIC_A: [html_response()]},
        )
        async with client:
            await client.get("http://example.com/")
        assert client.is_closed


class TestTlsHostnamePreserved:
    async def test_https_dial_uses_validated_ip_with_origin_sni(self) -> None:
        """The pool hands start_tls the ORIGIN hostname; the backend only
        provides the raw TCP stream — SNI/cert verification stay intact."""
        tls_hostnames: list[object] = []

        class SniffStream(httpcore.AsyncNetworkStream):
            def __init__(self, inner: httpcore.AsyncNetworkStream) -> None:
                self._inner = inner

            async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
                return await self._inner.read(max_bytes, timeout)

            async def write(self, buffer: bytes, timeout: float | None = None) -> None:
                await self._inner.write(buffer, timeout)

            async def aclose(self) -> None:
                await self._inner.aclose()

            async def start_tls(
                self,
                ssl_context: object,
                server_hostname: str | None = None,
                timeout: float | None = None,
            ) -> httpcore.AsyncNetworkStream:
                tls_hostnames.append(server_hostname)
                raise httpcore.ConnectError("stop here — sniff only")

        class SniffBackend(ScriptedBackend):
            async def connect_tcp(
                self,
                host: str,
                port: int,
                timeout: float | None = None,
                local_address: str | None = None,
                socket_options: object = None,
            ) -> httpcore.AsyncNetworkStream:
                stream = await super().connect_tcp(
                    host, port, timeout, local_address, socket_options
                )
                return SniffStream(stream)

        sniff = SniffBackend({PUBLIC_A: [html_response()]})

        backend = ValidatingNetworkBackend(
            guard=NetworkGuard(),
            resolver=fake_resolver({"example.com": [PUBLIC_A]}),
            dialer=sniff,
        )
        client = guarded_async_client(backend)
        async with client:
            with pytest.raises(httpx.ConnectError):
                await client.get("https://example.com/")
        assert sniff.dials == [(PUBLIC_A, 443)]
        assert tls_hostnames == ["example.com"]


class TestExceptionContract:
    """Typed exceptions carry structured fields and a readable ``__str__``."""

    def test_policy_blocked_carries_reason_and_url(self) -> None:
        exc = PolicyBlockedError(
            reason="Blocked: 169.254.169.254 is a private address",
            url="http://169.254.169.254/latest",
        )
        assert exc.reason == "Blocked: 169.254.169.254 is a private address"
        assert exc.url == "http://169.254.169.254/latest"
        assert "169.254.169.254" in str(exc)
        assert "http://169.254.169.254/latest" in str(exc)

    def test_hop_limit_error_carries_hops_and_max(self) -> None:
        exc = GuardedHttpError(
            message="Too many redirects", url="http://hop4.example/next", hops=4, max_hops=3
        )
        assert exc.url == "http://hop4.example/next"
        assert exc.hops == 4
        assert exc.max_hops == 3
        rendered = str(exc)
        assert "Too many redirects" in rendered
        assert "hop4.example" in rendered

    def test_guarded_error_defaults_are_neutral(self) -> None:
        exc = GuardedHttpError(message="boom")
        assert exc.url is None
        assert exc.hops is None
        assert exc.max_hops is None
        assert str(exc) == "boom"

    def test_policy_blocked_is_a_guarded_http_error(self) -> None:
        assert issubclass(PolicyBlockedError, GuardedHttpError)
