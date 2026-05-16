"""Tests for framework.providers.shared.errors."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from framework.core.llm_struct import LLMErrorInfo, LLMErrorKind


def _make_classify():
    """Import helpers fresh for each test to get clean module state."""
    from framework.providers.shared import errors
    return errors


class TestClassifyByString:
    """Tests for classify_by_string() — the string-scan fallback."""

    def test_connection_refused(self):
        mod = _make_classify()
        result = mod.classify_by_string("connection refused", "connection refused")
        assert result.kind == LLMErrorKind.CONNECTION
        assert result.should_retry is True

    def test_timeout(self):
        mod = _make_classify()
        result = mod.classify_by_string("timed out", "timed out")
        assert result.kind == LLMErrorKind.TIMEOUT
        assert result.should_retry is True

    def test_auth_401(self):
        mod = _make_classify()
        result = mod.classify_by_string("HTTP 401 Unauthorized", "HTTP 401 Unauthorized")
        assert result.kind == LLMErrorKind.AUTH
        assert result.should_retry is False

    def test_server_500(self):
        mod = _make_classify()
        result = mod.classify_by_string("internal 500 error", "internal 500 error")
        assert result.kind == LLMErrorKind.SERVER
        assert result.should_retry is True

    def test_unknown(self):
        mod = _make_classify()
        result = mod.classify_by_string("something unexpected", "something unexpected")
        assert result.kind == LLMErrorKind.UNKNOWN
        assert result.should_retry is False

    def test_rate_limit(self):
        mod = _make_classify()
        result = mod.classify_by_string("rate limit exceeded", "rate limit exceeded")
        assert result.kind == LLMErrorKind.RATE_LIMIT
        assert result.should_retry is True

    def test_quota(self):
        mod = _make_classify()
        result = mod.classify_by_string("insufficient_quota: rate limit", "insufficient_quota")
        assert result.kind == LLMErrorKind.QUOTA
        assert result.should_retry is False


class TestClassifyRateLimit:
    """Tests for classify_rate_limit() — RateLimitError handler."""

    def test_transient(self):
        mod = _make_classify()
        exc = MagicMock()
        exc.status_code = 429
        exc.body = {"error": {"type": "rate_limit_exceeded", "message": "too fast"}}
        result = mod.classify_rate_limit(exc, "rate limit")
        assert result.kind == LLMErrorKind.RATE_LIMIT
        assert result.should_retry is True
        assert result.status_code == 429

    def test_quota(self):
        mod = _make_classify()
        exc = MagicMock()
        exc.status_code = 429
        exc.body = {"error": {"type": "insufficient_quota", "message": "out of credits"}}
        result = mod.classify_rate_limit(exc, "quota")
        assert result.kind == LLMErrorKind.QUOTA
        assert result.should_retry is False

    def test_no_body_fallback(self):
        mod = _make_classify()
        exc = MagicMock()
        exc.status_code = 429
        exc.body = None
        result = mod.classify_rate_limit(exc, "rate limit")
        assert result.kind == LLMErrorKind.RATE_LIMIT
        assert result.should_retry is True


class TestClassifyApiStatus:
    """Tests for classify_api_status() — generic APIStatusError handler."""

    def test_429_rate_limit(self):
        mod = _make_classify()
        exc = MagicMock()
        exc.status_code = 429
        exc.body = {"error": {"type": "rate_limit_exceeded"}}
        result = mod.classify_api_status(exc, "too many requests")
        assert result.kind == LLMErrorKind.RATE_LIMIT
        assert result.should_retry is True

    def test_429_quota(self):
        mod = _make_classify()
        exc = MagicMock()
        exc.status_code = 429
        exc.body = {"error": {"type": "insufficient_quota"}}
        result = mod.classify_api_status(exc, "quota exceeded")
        assert result.kind == LLMErrorKind.QUOTA
        assert result.should_retry is False

    def test_401(self):
        mod = _make_classify()
        exc = MagicMock()
        exc.status_code = 401
        exc.body = None
        result = mod.classify_api_status(exc, "unauthorized")
        assert result.kind == LLMErrorKind.AUTH
        assert result.should_retry is False

    def test_403(self):
        mod = _make_classify()
        exc = MagicMock()
        exc.status_code = 403
        exc.body = None
        result = mod.classify_api_status(exc, "forbidden")
        assert result.kind == LLMErrorKind.AUTH
        assert result.should_retry is False

    def test_500(self):
        mod = _make_classify()
        exc = MagicMock()
        exc.status_code = 500
        exc.body = None
        result = mod.classify_api_status(exc, "server error")
        assert result.kind == LLMErrorKind.SERVER
        assert result.should_retry is True

    def test_unknown(self):
        mod = _make_classify()
        exc = MagicMock()
        exc.status_code = 404
        exc.body = None
        result = mod.classify_api_status(exc, "not found")
        assert result.kind == LLMErrorKind.UNKNOWN
        assert result.should_retry is False


class TestClassifyOpenaiErrorRouting:
    """Integration tests for classify_openai_error() routing."""

    def test_string_fallback_path(self):
        """Generic exception falls through to string scanning."""
        mod = _make_classify()
        with patch.object(mod, "HAS_OPENAI", False):
            result = mod.classify_openai_error(Exception("connection refused"))
        assert result.kind == LLMErrorKind.CONNECTION

    def test_timeout_routing(self):
        """Exception containing 'timeout' matches via string path."""
        mod = _make_classify()
        with patch.object(mod, "HAS_OPENAI", False):
            result = mod.classify_openai_error(Exception("timed out"))
        assert result.kind == LLMErrorKind.TIMEOUT

    def test_unknown_routing(self):
        """Completely unknown exception returns UNKNOWN."""
        mod = _make_classify()
        with patch.object(mod, "HAS_OPENAI", False):
            result = mod.classify_openai_error(Exception("xyz"))
        assert result.kind == LLMErrorKind.UNKNOWN
