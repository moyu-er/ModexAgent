"""Error classification for openai SDK exceptions.

classify_openai_error — isinstance-based classification with string-scan fallback.
No getattr/hasattr. All attribute access is on known openai exception types.
"""

from __future__ import annotations

import logging

from modex_agent.core.llm_struct import LLMErrorInfo, LLMErrorKind, is_content_filter_text

logger = logging.getLogger(__name__)

try:
    from openai import (
        APIConnectionError,
        APIStatusError,
        APITimeoutError,
        AuthenticationError,
        BadRequestError,
        InternalServerError,
        PermissionDeniedError,
        RateLimitError,
    )

    HAS_OPENAI = True
except ImportError:
    HAS_OPENAI = False


def classify_openai_error(exc: Exception) -> LLMErrorInfo:
    """Classify an exception from openai SDK into structured LLMErrorInfo.

    Priority:
    1. isinstance matching on openai exception types -> typed attribute access
    2. String-scan fallback for generic/non-openai exceptions

    Returns LLMErrorInfo with appropriate kind and should_retry.
    """
    message = str(exc)[:500]

    if HAS_OPENAI:
        # Tier 1: openai SDK typed exceptions
        if isinstance(exc, APITimeoutError):
            return LLMErrorInfo(kind=LLMErrorKind.TIMEOUT, message=message, provider="openai", should_retry=True)

        if isinstance(exc, APIConnectionError):
            return LLMErrorInfo(kind=LLMErrorKind.CONNECTION, message=message, provider="openai", should_retry=True)

        if isinstance(exc, InternalServerError):
            return LLMErrorInfo(
                kind=LLMErrorKind.SERVER,
                message=message,
                provider="openai",
                status_code=exc.status_code,
                should_retry=True,
            )

        if isinstance(exc, RateLimitError):
            return classify_rate_limit(exc, message)

        if isinstance(exc, AuthenticationError | PermissionDeniedError):
            return LLMErrorInfo(
                kind=LLMErrorKind.AUTH,
                message=message,
                provider="openai",
                status_code=exc.status_code,
                should_retry=False,
            )

        if isinstance(exc, BadRequestError):
            return LLMErrorInfo(
                kind=LLMErrorKind.INVALID_REQUEST,
                message=message,
                provider="openai",
                status_code=exc.status_code,
                should_retry=False,
            )

        # Any other APIStatusError — extract body for structured details
        if isinstance(exc, APIStatusError):
            return classify_api_status(exc, message)

    # Tier 2: String-scan fallback for non-openai exceptions
    return classify_by_string(str(exc).lower(), message)


def classify_rate_limit(exc, message: str) -> LLMErrorInfo:
    """Handle RateLimitError with quota vs. transient distinction."""
    body = exc.body
    if isinstance(body, dict):
        error_type = body.get("error", {}).get("type", "")
        if error_type and ("quota" in error_type.lower() or "insufficient" in error_type.lower()):
            return LLMErrorInfo(
                kind=LLMErrorKind.QUOTA, message=message, provider="openai", status_code=exc.status_code, should_retry=False
            )
    return LLMErrorInfo(
        kind=LLMErrorKind.RATE_LIMIT, message=message, provider="openai", status_code=exc.status_code, should_retry=True
    )


def classify_api_status(exc, message: str) -> LLMErrorInfo:
    """Classify generic APIStatusError by status code and body."""
    status_code = exc.status_code
    body = exc.body
    if isinstance(body, dict):
        error_type = body.get("error", {}).get("type", "")
        error_code = body.get("error", {}).get("code", "")
        if error_type and status_code == 429:
            if error_type == "insufficient_quota" or "quota" in error_type.lower():
                return LLMErrorInfo(
                    kind=LLMErrorKind.QUOTA, message=message, provider="openai", status_code=status_code, should_retry=False
                )
            return LLMErrorInfo(
                kind=LLMErrorKind.RATE_LIMIT, message=message, provider="openai", status_code=status_code, should_retry=True
            )
        # Content moderation surfaced in the response body (OpenAI marks the
        # error type/code as content_filter).
        if is_content_filter_text(f"{error_type} {error_code}".lower()):
            return LLMErrorInfo(
                kind=LLMErrorKind.CONTENT_FILTER, message=message, provider="openai", status_code=status_code, should_retry=False
            )

    if status_code in (401, 403):
        return LLMErrorInfo(kind=LLMErrorKind.AUTH, message=message, provider="openai", status_code=status_code, should_retry=False)
    if 500 <= status_code < 600:
        return LLMErrorInfo(kind=LLMErrorKind.SERVER, message=message, provider="openai", status_code=status_code, should_retry=True)
    if status_code == 429:
        return LLMErrorInfo(
            kind=LLMErrorKind.RATE_LIMIT, message=message, provider="openai", status_code=status_code, should_retry=True
        )

    return LLMErrorInfo(kind=LLMErrorKind.UNKNOWN, message=message, provider="openai", status_code=status_code, should_retry=False)


def classify_by_string(text: str, message: str) -> LLMErrorInfo:
    """String-scan fallback for non-openai exceptions."""
    if is_content_filter_text(text):
        return LLMErrorInfo(
            kind=LLMErrorKind.CONTENT_FILTER, message=message, provider="openai", should_retry=False
        )

    if "timeout" in text or "timed out" in text:
        return LLMErrorInfo(kind=LLMErrorKind.TIMEOUT, message=message, provider="openai", should_retry=True)

    if "rate" in text and "limit" in text:
        if "quota" in text or "insufficient" in text:
            return LLMErrorInfo(kind=LLMErrorKind.QUOTA, message=message, provider="openai", should_retry=False)
        return LLMErrorInfo(kind=LLMErrorKind.RATE_LIMIT, message=message, provider="openai", should_retry=True)

    if "auth" in text or "401" in text or "403" in text:
        return LLMErrorInfo(kind=LLMErrorKind.AUTH, message=message, provider="openai", should_retry=False)

    if any(code in text for code in ("500", "502", "503", "504")):
        return LLMErrorInfo(kind=LLMErrorKind.SERVER, message=message, provider="openai", should_retry=True)

    if "connection" in text or "connect" in text:
        return LLMErrorInfo(kind=LLMErrorKind.CONNECTION, message=message, provider="openai", should_retry=True)

    return LLMErrorInfo(kind=LLMErrorKind.UNKNOWN, message=message, provider="openai", should_retry=False)
