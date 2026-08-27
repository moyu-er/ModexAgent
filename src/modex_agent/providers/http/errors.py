"""Default HTTP error classifier shared by the three protocol engines.

``classify_http_error`` maps a raw HTTP status + response body (+ response
headers) to a structured :class:`~modex_agent.core.llm_struct.LLMErrorInfo`.
It is the *default* classifier: ``LLMProtocol.classify_http_error`` delegates
here, and a protocol engine may override it when its wire format carries
provider-specific signal that this status+body scan cannot see.

The classifier works on raw status and body bytes with no SDK dependency.
The marker vocabularies — context overflow and content filter — are shared
with ``core.llm_struct`` so every error path agrees on what those failures
look like.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from math import isfinite

from modex_agent.core.llm_struct import (
    LLMErrorInfo,
    LLMErrorKind,
    is_content_filter_text,
    is_context_overflow_text,
)

# Error bodies are scanned, never stored — bound the work so a hostile or
# buggy gateway returning megabytes cannot stall the error path.
_MAX_ERROR_BODY_BYTES = 64 * 1024

# 429 body wording meaning "the account is out of budget" (retrying cannot
# help and only burns tokens) rather than a transient request-rate limit.
# "insufficient_quota" is covered by the "quota" substring.
_QUOTA_MARKERS = ("quota", "billing", "credits")


def classify_http_error(
    status: int,
    body: bytes,
    provider: str,
    headers: Mapping[str, str] | None = None,
) -> LLMErrorInfo:
    """Classify a non-2xx HTTP response into a structured ``LLMErrorInfo``.

    The status drives the decision tree; the decoded body refines it:

    - 401/403 → ``AUTH`` (never retried)
    - 429 → quota wording in the body → ``QUOTA`` (never retried — the
      account is out of budget); otherwise ``RATE_LIMIT`` (retried)
    - 400/413 → context-overflow markers → ``INVALID_REQUEST``; content
      filter markers → ``CONTENT_FILTER``; otherwise ``INVALID_REQUEST``
      (none of the three retried)
    - 5xx → ``SERVER`` (retried)
    - anything else → ``UNKNOWN`` (never retried)

    The body is truncated to 64 KiB and parsed as one of three error
    shapes — OpenAI ``{"error": {"message", "type", "code"}}``, Anthropic
    ``{"type": "error", "error": {"type", "message"}}``, or bare non-JSON
    text (the whole decoded body becomes the message). A parse failure
    never raises; the message falls back to ``"HTTP {status}"``.

    A strictly positive ``Retry-After`` response header (seconds or
    HTTP-date form, header name matched case-insensitively) is written to
    ``retry_after_seconds``.
    """
    message, detail = _parse_error_body(body)
    detail = detail.lower()
    retry_after = _retry_after_seconds(headers)

    if status in (401, 403):
        kind, should_retry = LLMErrorKind.AUTH, False
    elif status == 429:
        if any(marker in detail for marker in _QUOTA_MARKERS):
            kind, should_retry = LLMErrorKind.QUOTA, False
        else:
            kind, should_retry = LLMErrorKind.RATE_LIMIT, True
    elif status in (400, 413):
        if is_context_overflow_text(detail):
            kind, should_retry = LLMErrorKind.INVALID_REQUEST, False
        elif is_content_filter_text(detail):
            kind, should_retry = LLMErrorKind.CONTENT_FILTER, False
        else:
            kind, should_retry = LLMErrorKind.INVALID_REQUEST, False
    elif 500 <= status <= 599:
        kind, should_retry = LLMErrorKind.SERVER, True
    else:
        kind, should_retry = LLMErrorKind.UNKNOWN, False

    return LLMErrorInfo(
        kind=kind,
        message=message if message else f"HTTP {status}",
        provider=provider,
        status_code=status,
        retry_after_seconds=retry_after,
        should_retry=should_retry,
    )


def _parse_error_body(body: bytes) -> tuple[str | None, str]:
    """Extract ``(message, scan_text)`` from an error body.

    ``message`` is the provider-reported error message (``None`` when the
    body yields none). ``scan_text`` is the text the marker checks run on:
    the joined ``code``/``type``/``message`` fields for the two structured
    error shapes, the raw decoded text otherwise. Binary garbage that does
    not decode as UTF-8 yields ``(None, "")``.
    """
    truncated = body[:_MAX_ERROR_BODY_BYTES]
    try:
        text = truncated.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None, ""
    try:
        parsed: object = json.loads(text)
    except ValueError:
        # Bare-text error body: the whole decoded body is the message.
        return (text or None), text
    if not isinstance(parsed, dict):
        return None, text
    error_obj: object = parsed.get("error")
    if not isinstance(error_obj, dict):
        return None, text
    message = _as_text(error_obj.get("message"))
    code = _as_text(error_obj.get("code"))
    error_type = _as_text(error_obj.get("type"))
    detail = " ".join(part for part in (code, error_type, message) if part)
    return message, detail


def _as_text(value: object) -> str | None:
    """Coerce an untrusted JSON error field to displayable text."""
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    return str(value)


def _retry_after_seconds(headers: Mapping[str, str] | None) -> float | None:
    """Read a strictly positive ``Retry-After`` value from response headers.

    The header name is matched case-insensitively; ``None`` when absent.
    """
    if headers is None:
        return None
    raw = next((value for key, value in headers.items() if key.lower() == "retry-after"), None)
    if raw is None:
        return None
    return _parse_retry_after(raw)


def _parse_retry_after(value: str) -> float | None:
    """Parse one ``Retry-After`` value in seconds or HTTP-date form.

    Only strictly positive, finite deltas are returned. Negative or zero
    values, non-finite numbers, unparseable text, and dates in the past
    all yield ``None``. A date without a timezone is read as UTC.
    """
    try:
        seconds = float(value)
    except ValueError:
        seconds = None
    if seconds is not None:
        return seconds if isfinite(seconds) and seconds > 0.0 else None
    try:
        retry_at = parsedate_to_datetime(value)
    except ValueError:
        return None
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=UTC)
    delta = (retry_at - datetime.now(UTC)).total_seconds()
    return delta if delta > 0.0 else None
