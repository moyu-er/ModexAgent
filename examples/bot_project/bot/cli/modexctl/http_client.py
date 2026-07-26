"""HTTP client for ``modexctl`` → control server communication (T04).

A thin sync wrapper around :mod:`httpx` that posts a :class:`HistoryRequest`
to ``POST /api/control/history`` on the bot's control server and returns
the parsed :class:`HistoryResult`.

The base URL comes from the ``MODEX_CONTROL_ORIGIN`` env var. It MUST be a
loopback origin (``127.0.0.1`` / ``localhost`` / ``::1``) — the control
server is a local-only service and the CLI rejects non-loopback origins
before making any request (defence-in-depth against SSRF).

The client is sync because the CLI is a sync Typer app. ``httpx.Client``
is used (not ``AsyncClient``) and closed after each request so no
connection pool leaks across invocations.
"""

from __future__ import annotations

import os
from urllib.parse import urlparse

import httpx

from bot.control.models import (
    ControlError,
    HistoryRequest,
    HistoryResult,
    SendRequest,
    SendResult,
)

#: Environment variable providing the control server base URL.
MODEX_CONTROL_ORIGIN_ENV: str = "MODEX_CONTROL_ORIGIN"

#: Default history endpoint path on the control server.
_CONTROL_HISTORY_PATH: str = "/api/control/history"

#: Send endpoint path on the control server (T06).
_CONTROL_SEND_PATH: str = "/api/control/send"

#: Loopback hostnames accepted by the origin validator.
_LOOPBACK_HOSTS: frozenset[str] = frozenset(
    {"127.0.0.1", "localhost", "::1", "[::1]"}
)


class ControlClientError(Exception):
    """Raised when the control server returns an error or is unreachable.

    Carries the HTTP status (when available) and the parsed
    :class:`ControlError` body (when the server returned one) so the CLI
    can surface a meaningful message.
    """

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        error: ControlError | None = None,
    ) -> None:
        self.status: int | None = status
        self.error: ControlError | None = error
        super().__init__(message)


def _validate_loopback_origin(origin: str) -> str:
    """Return the origin if loopback, else raise :class:`ControlClientError`.

    Strips the trailing path component if present (the origin is a base URL;
    the path is appended by the caller). Accepts ``http://`` and ``https://``
    schemes only.
    """
    parsed = urlparse(origin)
    host = parsed.hostname or ""
    # urlparse lowercases the hostname; bracket-strip for IPv6 ``[::1]``.
    host_lower = host.lower().strip("[]")
    if host_lower not in _LOOPBACK_HOSTS:
        raise ControlClientError(
            f"Control origin {origin!r} is not loopback; "
            f"MODEX_CONTROL_ORIGIN must point at 127.0.0.1 / localhost / ::1"
        )
    if parsed.scheme not in ("http", "https"):
        raise ControlClientError(
            f"Control origin {origin!r} has invalid scheme {parsed.scheme!r}; "
            f"expected http or https"
        )
    # Re-emit as ``scheme://host:port`` (drop any path / query / fragment).
    netloc = parsed.netloc
    if not netloc:
        netloc = host_lower
    return f"{parsed.scheme}://{netloc}"


def get_control_origin() -> str:
    """Read + validate ``MODEX_CONTROL_ORIGIN``.

    Raises :class:`ControlClientError` when unset or non-loopback.
    """
    raw = os.environ.get(MODEX_CONTROL_ORIGIN_ENV, "")
    if not raw:
        raise ControlClientError(
            f"Environment variable {MODEX_CONTROL_ORIGIN_ENV} is not set"
        )
    return _validate_loopback_origin(raw)


def fetch_history(request: HistoryRequest) -> HistoryResult:
    """POST a :class:`HistoryRequest` and return the :class:`HistoryResult`.

    Uses a short-lived :class:`httpx.Client` (one request, then closed).
    Raises :class:`ControlClientError` on any failure: non-loopback origin,
    connection error, non-200 status, or malformed response body.
    """
    origin = get_control_origin()
    url = origin + _CONTROL_HISTORY_PATH
    payload = request.model_dump(mode="json")

    try:
        with httpx.Client(timeout=httpx.Timeout(connect=1.0, read=10.0, write=10.0, pool=1.0)) as client:
            resp = client.post(url, json=payload)
    except httpx.RequestError as exc:
        raise ControlClientError(
            f"Failed to connect to control server at {url}: {exc}"
        ) from exc

    if resp.status_code != 200:
        _raise_for_error_response(resp)

    try:
        body = resp.json()
    except ValueError as exc:
        raise ControlClientError(
            f"Control server returned non-JSON body: {exc}"
        ) from exc

    try:
        return HistoryResult.model_validate(body)
    except Exception as exc:
        raise ControlClientError(
            f"Control server returned malformed HistoryResult: {exc}"
        ) from exc


def fetch_send(request: SendRequest) -> SendResult:
    """POST a :class:`SendRequest` and return the :class:`SendResult`.

    Same loopback-origin validation, short-lived client, and error
    classification as :func:`fetch_history`.
    """
    origin = get_control_origin()
    url = origin + _CONTROL_SEND_PATH
    payload = request.model_dump(mode="json")

    try:
        with httpx.Client(timeout=httpx.Timeout(connect=1.0, read=10.0, write=10.0, pool=1.0)) as client:
            resp = client.post(url, json=payload)
    except httpx.RequestError as exc:
        raise ControlClientError(
            f"Failed to connect to control server at {url}: {exc}"
        ) from exc

    if resp.status_code != 200:
        _raise_for_error_response(resp)

    try:
        body = resp.json()
    except ValueError as exc:
        raise ControlClientError(
            f"Control server returned non-JSON body: {exc}"
        ) from exc

    try:
        return SendResult.model_validate(body)
    except Exception as exc:
        raise ControlClientError(
            f"Control server returned malformed SendResult: {exc}"
        ) from exc


def _raise_for_error_response(resp: httpx.Response) -> None:
    """Parse a :class:`ControlError` from *resp* and raise it.

    Falls back to a generic error when the body is not a valid
    :class:`ControlError` (e.g. an aiohttp 500 HTML traceback).
    """
    try:
        body = resp.json()
        error = ControlError.model_validate(body)
    except Exception:
        raise ControlClientError(
            f"Control server returned HTTP {resp.status_code}: "
            f"{resp.text[:200]}",
            status=resp.status_code,
        ) from None
    raise ControlClientError(
        f"[{resp.status_code}] {error.code}: {error.message}",
        status=resp.status_code,
        error=error,
    )


__all__ = [
    "ControlClientError",
    "MODEX_CONTROL_ORIGIN_ENV",
    "fetch_history",
    "fetch_send",
    "get_control_origin",
]
