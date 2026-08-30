"""Tests for modex_agent.providers.http.errors.classify_http_error."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import pytest

from modex_agent.core.llm_struct import LLMErrorKind
from modex_agent.providers.http.errors import classify_http_error

PROVIDER = "openai-compat"


def _openai_body(
    message: str = "request failed",
    err_type: str = "invalid_request_error",
    code: object = None,
) -> bytes:
    """OpenAI error shape: {"error": {"message", "type", "code"}}."""
    return json.dumps({"error": {"message": message, "type": err_type, "code": code}}).encode()


def _anthropic_body(message: str = "request failed", err_type: str = "api_error") -> bytes:
    """Anthropic error shape: {"type": "error", "error": {"type", "message"}}."""
    return json.dumps({"type": "error", "error": {"type": err_type, "message": message}}).encode()


_BARE = b"request failed"

# Statuses with a neutral body (no quota/overflow/filter markers): the
# status alone decides kind + should_retry, across all three body shapes.
_STATUS_CASES = [
    (401, LLMErrorKind.AUTH, False),
    (403, LLMErrorKind.AUTH, False),
    (429, LLMErrorKind.RATE_LIMIT, True),
    (400, LLMErrorKind.INVALID_REQUEST, False),
    (413, LLMErrorKind.INVALID_REQUEST, False),
    (500, LLMErrorKind.SERVER, True),
    (502, LLMErrorKind.SERVER, True),
    (418, LLMErrorKind.UNKNOWN, False),
]


class TestStatusShapeMatrix:
    """Status-driven classification across the three error-body shapes."""

    @pytest.mark.parametrize(("status", "kind", "should_retry"), _STATUS_CASES)
    @pytest.mark.parametrize(
        "body",
        [
            pytest.param(_openai_body(), id="openai-json"),
            pytest.param(_anthropic_body(), id="anthropic-json"),
            pytest.param(_BARE, id="bare-text"),
        ],
    )
    def test_matrix(
        self,
        status: int,
        kind: LLMErrorKind,
        should_retry: bool,
        body: bytes,
    ) -> None:
        info = classify_http_error(status, body, PROVIDER)
        assert info.kind == kind
        assert info.should_retry is should_retry
        assert info.status_code == status
        assert info.provider == PROVIDER
        assert info.message == "request failed"
        assert info.retry_after_seconds is None


class TestQuotaDetection:
    """429 with budget wording is QUOTA (terminal); otherwise RATE_LIMIT."""

    @pytest.mark.parametrize(
        "body",
        [
            pytest.param(
                _openai_body(
                    "You exceeded your current quota, please check your plan and billing details",
                    code="insufficient_quota",
                ),
                id="openai-insufficient-quota",
            ),
            pytest.param(_openai_body("insufficient_quota"), id="openai-code-less-message"),
            pytest.param(_openai_body("You have insufficient credits"), id="openai-insufficient-credits"),
            pytest.param(_anthropic_body("Your account has insufficient credits"), id="anthropic-credits"),
            pytest.param(b"monthly billing limit reached", id="bare-billing"),
            pytest.param(b"quota exceeded for this API key", id="bare-quota"),
        ],
    )
    def test_quota_bodies(self, body: bytes) -> None:
        info = classify_http_error(429, body, PROVIDER)
        assert info.kind == LLMErrorKind.QUOTA
        assert info.should_retry is False

    def test_transient_rate_limit_stays_retryable(self) -> None:
        info = classify_http_error(429, _openai_body("Rate limit exceeded, please retry later"), PROVIDER)
        assert info.kind == LLMErrorKind.RATE_LIMIT
        assert info.should_retry is True


class TestContextOverflowAndContentFilter:
    """400/413 marker refinement, with overflow taking priority over filter."""

    @pytest.mark.parametrize(
        ("body", "kind"),
        [
            pytest.param(
                _openai_body(
                    "This model's maximum context length is 4096 tokens. "
                    "However, you requested 5000 tokens"
                ),
                LLMErrorKind.INVALID_REQUEST,
                id="openai-context-length",
            ),
            pytest.param(
                _anthropic_body("prompt is too long: 21000 tokens > 2000 maximum"),
                LLMErrorKind.INVALID_REQUEST,
                id="anthropic-prompt-too-long",
            ),
            pytest.param(b"Payload Too Large", LLMErrorKind.INVALID_REQUEST, id="bare-413"),
            pytest.param(
                _openai_body("output blocked", err_type="content_filter"),
                LLMErrorKind.CONTENT_FILTER,
                id="openai-filter-type",
            ),
            pytest.param(
                _openai_body(
                    "Your request was rejected as a result of our content management policy"
                ),
                LLMErrorKind.CONTENT_FILTER,
                id="openai-filter-message",
            ),
            pytest.param(b"output new_sensitive (1027)", LLMErrorKind.CONTENT_FILTER, id="glm-sensitive"),
        ],
    )
    def test_marker_bodies(self, body: bytes, kind: LLMErrorKind) -> None:
        info = classify_http_error(400, body, PROVIDER)
        assert info.kind == kind
        assert info.should_retry is False

    def test_context_overflow_takes_priority_over_content_filter(self) -> None:
        """Both markers present → INVALID_REQUEST (overflow checked first)."""
        body = _openai_body(
            "This model's maximum context length is 4096 tokens",
            err_type="content_filter",
        )
        info = classify_http_error(400, body, PROVIDER)
        assert info.kind == LLMErrorKind.INVALID_REQUEST
        assert info.should_retry is False

    def test_overflow_at_413(self) -> None:
        info = classify_http_error(413, b"request payload too large for this model", PROVIDER)
        assert info.kind == LLMErrorKind.INVALID_REQUEST
        assert info.should_retry is False


class TestRetryAfter:
    """Retry-After parsing: seconds, HTTP-date, invalid, non-positive, missing."""

    def test_seconds_form(self) -> None:
        info = classify_http_error(429, _openai_body(), PROVIDER, {"Retry-After": "120"})
        assert info.retry_after_seconds == 120.0
        assert info.kind == LLMErrorKind.RATE_LIMIT

    @pytest.mark.parametrize("key", ["Retry-After", "retry-after", "RETRY-AFTER", "rEtRy-aFtEr"])
    def test_header_name_case_insensitive(self, key: str) -> None:
        info = classify_http_error(429, _openai_body(), PROVIDER, {key: "30"})
        assert info.retry_after_seconds == 30.0

    def test_http_date_future(self) -> None:
        future = datetime.now(UTC) + timedelta(hours=2)
        info = classify_http_error(429, _openai_body(), PROVIDER, {"Retry-After": format_datetime(future)})
        assert info.retry_after_seconds is not None
        assert 0 < info.retry_after_seconds <= 7200

    def test_http_date_without_timezone_read_as_utc(self) -> None:
        info = classify_http_error(429, _openai_body(), PROVIDER, {"Retry-After": "Wed, 21 Oct 2099 07:28:00"})
        assert info.retry_after_seconds is not None
        assert info.retry_after_seconds > 0

    def test_http_date_past_not_written(self) -> None:
        info = classify_http_error(
            429, _openai_body(), PROVIDER, {"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"}
        )
        assert info.retry_after_seconds is None

    @pytest.mark.parametrize("value", ["soon", "in 5 minutes", "", "Wed, 99 Foo"])
    def test_invalid_value_not_written(self, value: str) -> None:
        info = classify_http_error(429, _openai_body(), PROVIDER, {"Retry-After": value})
        assert info.retry_after_seconds is None

    @pytest.mark.parametrize("value", ["0", "-5", "-0"])
    def test_non_positive_seconds_not_written(self, value: str) -> None:
        info = classify_http_error(429, _openai_body(), PROVIDER, {"Retry-After": value})
        assert info.retry_after_seconds is None

    def test_missing_header(self) -> None:
        info = classify_http_error(429, _openai_body(), PROVIDER, {"Content-Type": "application/json"})
        assert info.retry_after_seconds is None

    def test_headers_none_default(self) -> None:
        info = classify_http_error(429, _openai_body(), PROVIDER)
        assert info.retry_after_seconds is None

    def test_retry_after_on_server_error(self) -> None:
        info = classify_http_error(503, b"overloaded", PROVIDER, {"Retry-After": "60"})
        assert info.kind == LLMErrorKind.SERVER
        assert info.should_retry is True
        assert info.retry_after_seconds == 60.0


class TestBodyRobustness:
    """Empty, binary, and oversized bodies never raise and classify by status."""

    @pytest.mark.parametrize(("status", "kind"), [(429, LLMErrorKind.RATE_LIMIT), (500, LLMErrorKind.SERVER)])
    def test_empty_body(self, status: int, kind: LLMErrorKind) -> None:
        info = classify_http_error(status, b"", PROVIDER)
        assert info.kind == kind
        assert info.message == f"HTTP {status}"

    def test_binary_garbage_beyond_64k_no_raise(self) -> None:
        info = classify_http_error(500, bytes(range(256)) * 512, PROVIDER)
        assert info.kind == LLMErrorKind.SERVER
        assert info.should_retry is True
        assert info.message == "HTTP 500"

    def test_binary_garbage_at_429_is_rate_limit(self) -> None:
        info = classify_http_error(429, bytes(range(256)) * 512, PROVIDER)
        assert info.kind == LLMErrorKind.RATE_LIMIT
        assert info.message == "HTTP 429"

    def test_truncated_json_body_classifies_by_status(self) -> None:
        """A >64 KiB body cut mid-JSON fails parsing but still classifies."""
        body = b'{"error": {"message": "rate limited ' + b"x" * 70000
        info = classify_http_error(429, body, PROVIDER)
        assert info.kind == LLMErrorKind.RATE_LIMIT
        assert info.should_retry is True
        assert info.message.startswith(b'{"error": {"message": "rate limited '.decode())

    def test_quota_word_beyond_64k_boundary_is_invisible(self) -> None:
        body = b'{"error": {"message": "' + b"x" * 70000 + b' insufficient_quota"}}'
        info = classify_http_error(429, body, PROVIDER)
        assert info.kind == LLMErrorKind.RATE_LIMIT

    def test_quota_word_within_64k_is_detected(self) -> None:
        body = b'{"error": {"message": "insufficient_quota ' + b"x" * 70000
        info = classify_http_error(429, body, PROVIDER)
        assert info.kind == LLMErrorKind.QUOTA
        assert info.should_retry is False


class TestMessageExtraction:
    """Message comes from the provider error body; fallback is HTTP {status}."""

    def test_openai_message(self) -> None:
        info = classify_http_error(401, _openai_body("Incorrect API key provided"), PROVIDER)
        assert info.message == "Incorrect API key provided"

    def test_anthropic_message(self) -> None:
        info = classify_http_error(401, _anthropic_body("invalid x-api-key"), PROVIDER)
        assert info.message == "invalid x-api-key"

    def test_bare_text_message(self) -> None:
        info = classify_http_error(502, b"upstream connect error", PROVIDER)
        assert info.message == "upstream connect error"

    def test_error_object_without_message_falls_back(self) -> None:
        info = classify_http_error(400, b'{"error": {"type": "invalid_request_error"}}', PROVIDER)
        assert info.message == "HTTP 400"

    def test_json_without_error_key_falls_back(self) -> None:
        info = classify_http_error(500, b'{"ok": false}', PROVIDER)
        assert info.message == "HTTP 500"
