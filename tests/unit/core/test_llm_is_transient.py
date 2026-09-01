"""Tests for ``LLMProvider._is_transient`` retry classification.

Migrated from tests/unit/media/test_multimedia_pipeline.py (2026-09) when the
dormant MediaProcessor family was removed — these tests cover the live
provider retry classifier, which merely lived in the same legacy file.
"""

from __future__ import annotations

from modex_agent.core.provider import LLMProvider


class TestIsTransient:
    def test_internal_server_error(self):
        assert LLMProvider._is_transient(  # noqa: SLF001
            Exception("litellm.InternalServerError: Empty or invalid response")
        ) is True

    def test_empty_response(self):
        assert LLMProvider._is_transient(Exception("Empty response from API")) is True  # noqa: SLF001

    def test_invalid_response(self):
        assert LLMProvider._is_transient(  # noqa: SLF001
            Exception("invalid response from LLM endpoint")
        ) is True

    def test_existing_markers_still_work(self):
        assert LLMProvider._is_transient(Exception("429 Too Many Requests")) is True  # noqa: SLF001
        assert LLMProvider._is_transient(Exception("502 Bad Gateway")) is True  # noqa: SLF001
        assert LLMProvider._is_transient(Exception("rate limit exceeded")) is True  # noqa: SLF001

    def test_non_transient_not_matched(self):
        assert LLMProvider._is_transient(Exception("invalid api key")) is False  # noqa: SLF001
        assert LLMProvider._is_transient(Exception("model not found")) is False  # noqa: SLF001

    def test_billing_error_not_retryable(self):
        assert LLMProvider._is_transient(  # noqa: SLF001
            Exception("500 server error insufficient_quota")
        ) is False
