"""Tests for LLM error types and timeout response builder."""

import pytest

from modex_agent.core.llm_struct import (
    LLMErrorInfo,
    LLMErrorKind,
    LLMProviderKind,
    LLMTimeoutPolicy,
    RuntimeSafetyPolicy,
    TurnTimeoutPolicy,
    build_timeout_response,
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
