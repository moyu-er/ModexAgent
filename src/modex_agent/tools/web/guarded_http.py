"""Guarded HTTP orchestration — client factory, redirects, fetch, errors.

Fetch half of the guarded HTTP stack; the resolver/backend/httpcore
adaptation lives in ``guarded_transport``. This module owns the
user-facing surface: the proxy-free guarded client factory (explicit
``proxy``/``mounts`` arguments bypass a custom top-level transport, so
the factory accepts neither, and ``trust_env=False`` keeps environment
proxies from mounting over the guarded transport), manual redirects
with a policy check on every hop, and the one-shot guarded fetch.

WebReader uses this independently of sandbox enablement. These checks do not
cover arbitrary shell, MCP, or external-provider networking, and fetching a
fully read response here imposes no response-size limit.
"""

from __future__ import annotations

from collections.abc import Mapping

import httpx

from modex_agent.sandbox.guard_network import NetworkGuard
from modex_agent.tools.web.guarded_transport import (
    GuardedAsyncTransport,
    ValidatingNetworkBackend,
)

__all__ = [
    "DEFAULT_MAX_HOPS",
    "GuardedHttpError",
    "PolicyBlockedError",
    "follow_guarded",
    "guarded_async_client",
    "guarded_fetch",
]

DEFAULT_MAX_HOPS = 5


class GuardedHttpError(Exception):
    """A guarded fetch failed for a non-policy reason (hop limit, malformed URL).

    ``message`` is the human-readable summary; ``url`` / ``hops`` /
    ``max_hops`` carry the structured context when they apply.
    """

    def __init__(
        self,
        message: str,
        *,
        url: str | None = None,
        hops: int | None = None,
        max_hops: int | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.url = url
        self.hops = hops
        self.max_hops = max_hops

    def __str__(self) -> str:
        rendered = self.message
        if self.url is not None:
            rendered += f" (url: {self.url}"
            if self.hops is not None and self.max_hops is not None:
                rendered += f", hops: {self.hops}/{self.max_hops}"
            rendered += ")"
        return rendered


class PolicyBlockedError(GuardedHttpError):
    """The target (or a redirect hop) violated the SSRF policy.

    ``reason`` is the guard's own denial wording.
    """

    def __init__(self, reason: str, *, url: str) -> None:
        super().__init__(f"SSRF: {reason}", url=url)
        self.reason = reason

    def __str__(self) -> str:
        return f"SSRF: {self.reason} (url: {self.url})"


def guarded_async_client(
    backend: ValidatingNetworkBackend,
    *,
    timeout: float = 20.0,
    headers: Mapping[str, str] | None = None,
) -> httpx.AsyncClient:
    """Build the guarded client. No ``proxy``/``mounts`` parameters exist
    by design: both would route around the guarded transport. ``trust_env``
    is False so environment proxy variables cannot mount over it either."""
    return httpx.AsyncClient(
        transport=GuardedAsyncTransport(backend),
        timeout=timeout,
        headers=dict(headers) if headers is not None else None,
        trust_env=False,
    )


async def follow_guarded(
    client: httpx.AsyncClient,
    request: httpx.Request,
    *,
    max_hops: int = DEFAULT_MAX_HOPS,
    guard: NetworkGuard | None = None,
) -> httpx.Response:
    """Send with manual redirects: each URL (initial + every hop) passes
    the static guard before sending; httpx's sanitized ``next_request``
    is reused so redirect header hygiene is preserved."""
    policy = guard if guard is not None else NetworkGuard()
    hops = 0
    response: httpx.Response | None = None
    try:
        while True:
            decision = policy.evaluate_url(str(request.url))
            if not decision.allowed:
                raise PolicyBlockedError(decision.reason, url=str(request.url))
            response = await client.send(request, follow_redirects=False)
            next_request = response.next_request
            if next_request is None:
                final, response = response, None
                return final
            if hops >= max_hops:
                raise GuardedHttpError(
                    f"Too many redirects: exceeded {max_hops} hops",
                    url=str(next_request.url),
                    hops=hops,
                    max_hops=max_hops,
                )
            hops += 1
            await response.aclose()
            response = None
            request = next_request
    finally:
        if response is not None:
            await response.aclose()


async def guarded_fetch(
    url: str,
    *,
    timeout: float,
    backend: ValidatingNetworkBackend,
    max_hops: int = DEFAULT_MAX_HOPS,
    headers: Mapping[str, str] | None = None,
) -> httpx.Response:
    """One guarded GET, fully read, with the client closed on every path."""
    client = guarded_async_client(backend, timeout=timeout, headers=headers)
    try:
        request = client.build_request("GET", url)
        response = await follow_guarded(client, request, max_hops=max_hops, guard=backend.guard)
        await response.aread()
        return response
    finally:
        await client.aclose()
