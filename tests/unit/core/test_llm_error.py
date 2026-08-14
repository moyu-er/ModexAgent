"""Tests for LLM error types, classification, and timeout response builder."""

import pytest

from modex_agent.core.llm_struct import (
    LLMErrorInfo,
    LLMErrorKind,
    LLMProviderKind,
    LLMTimeoutPolicy,
    RuntimeSafetyPolicy,
    TurnTimeoutPolicy,
    build_timeout_response,
    classify_litellm_error,
)
from modex_agent.core.types import LLMResponse


class TestLLMErrorKind:
    def test_all_kinds_defined(self):
        assert LLMErrorKind.TIMEOUT.value == "timeout"
        assert LLMErrorKind.RATE_LIMIT.value == "rate_limit"
        assert LLMErrorKind.AUTH.value == "auth"
        assert LLMErrorKind.QUOTA.value == "quota"
        assert LLMErrorKind.CONNECTION.value == "connection"
        assert LLMErrorKind.SERVER.value == "server"
        assert LLMErrorKind.INVALID_REQUEST.value == "invalid_request"
        assert LLMErrorKind.UNKNOWN.value == "unknown"


class TestLLMErrorInfo:
    def test_frozen_dataclass(self):
        info = LLMErrorInfo(
            kind=LLMErrorKind.TIMEOUT,
            message="Request timed out",
            provider="openai",
            should_retry=True,
        )
        with pytest.raises(Exception):
            info.kind = LLMErrorKind.UNKNOWN  # type: ignore[misc]

    def test_defaults(self):
        info = LLMErrorInfo(kind=LLMErrorKind.UNKNOWN, message="test")
        assert info.provider is None
        assert info.status_code is None
        assert info.retry_after_seconds is None
        assert info.should_retry is False

    def test_retry_after(self):
        info = LLMErrorInfo(
            kind=LLMErrorKind.RATE_LIMIT,
            message="Rate limited",
            retry_after_seconds=30.0,
            should_retry=True,
        )
        assert info.retry_after_seconds == 30.0


class TestProviderKind:
    def test_provider_kinds(self):
        assert LLMProviderKind.OPENAI.value == "openai"
        assert LLMProviderKind.ANTHROPIC.value == "anthropic"
        assert LLMProviderKind.LITELLM.value == "litellm"


class TestSafetyPolicyDefaults:
    def test_runtime_safety_policy_defaults(self):
        policy = RuntimeSafetyPolicy()
        assert isinstance(policy.llm, LLMTimeoutPolicy)
        assert isinstance(policy.turn, TurnTimeoutPolicy)

    def test_llm_timeout_policy_defaults(self):
        policy = LLMTimeoutPolicy()
        assert policy.request_timeout_seconds is None
        assert policy.stream_idle_timeout_seconds is None


class TestBuildTimeoutResponse:
    def test_build_timeout_response(self):
        resp = build_timeout_response(provider="litellm", message="timed out")
        assert isinstance(resp, LLMResponse)
        assert resp.finish_reason == "error"
        assert resp.error == "timed out"
        assert resp.error_info is not None
        assert resp.error_info.kind == LLMErrorKind.TIMEOUT

    def test_build_timeout_response_partial_content(self):
        resp = build_timeout_response(
            provider="litellm",
            message="stream idle",
            partial_content="partial reply...",
        )
        assert resp.content == "partial reply..."
        assert resp.finish_reason == "error"


class TestClassifyLitellmError:
    def test_timeout_error(self):
        exc = Exception("Request timed out")
        info = classify_litellm_error(exc)
        assert info.kind == LLMErrorKind.TIMEOUT
        assert info.should_retry is True

    def test_rate_limit_error(self):
        exc = Exception("429 rate limit exceeded")
        info = classify_litellm_error(exc)
        assert info.kind == LLMErrorKind.RATE_LIMIT
        assert info.should_retry is True

    def test_auth_error(self):
        exc = Exception("401 unauthorized")
        info = classify_litellm_error(exc)
        assert info.kind == LLMErrorKind.AUTH
        assert info.should_retry is False

    def test_connection_error(self):
        exc = Exception("connection reset by peer")
        info = classify_litellm_error(exc)
        assert info.kind == LLMErrorKind.CONNECTION
        assert info.should_retry is True

    def test_server_error_500(self):
        exc = Exception("500 internal server error")
        info = classify_litellm_error(exc)
        assert info.kind == LLMErrorKind.SERVER
        assert info.should_retry is True

    def test_server_error_503(self):
        exc = Exception("503 service unavailable")
        info = classify_litellm_error(exc)
        assert info.kind == LLMErrorKind.SERVER
        assert info.should_retry is True

    def test_unknown_error(self):
        exc = Exception("something completely unexpected")
        info = classify_litellm_error(exc)
        assert info.kind == LLMErrorKind.UNKNOWN
        assert info.should_retry is False

    def test_classify_preserves_message(self):
        msg = "Connection timeout after 30s"
        exc = Exception(msg)
        info = classify_litellm_error(exc)
        assert msg in info.message or info.message == str(exc)
