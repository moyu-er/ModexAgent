"""Tests for CircuitBreaker state machine (P1 Step 15.1)."""

import asyncio

import pytest

from framework.core.llm_error import (
    CircuitBreaker,
    CircuitBreakerPolicy,
    CircuitBreakerState,
    LLMErrorInfo,
    LLMErrorKind,
)


class TestCircuitBreakerStates:
    """Circuit breaker three-state machine tests."""

    @pytest.fixture
    def breaker(self):
        return CircuitBreaker(
            name="test",
            failure_threshold=3,
            cooldown_seconds=1.0,
            enabled=True,
        )

    @pytest.mark.asyncio
    async def test_initial_state_is_closed(self, breaker):
        assert breaker.state == CircuitBreakerState.CLOSED
        assert breaker.is_open() is False
        assert breaker.is_half_open() is False

    @pytest.mark.asyncio
    async def test_disabled_breaker_always_allows(self):
        breaker = CircuitBreaker(enabled=False)
        assert breaker.is_open() is False
        assert await breaker.allow_request() is True

    @pytest.mark.asyncio
    async def test_closed_to_open_after_threshold(self, breaker):
        error = LLMErrorInfo(kind=LLMErrorKind.TIMEOUT, message="timeout")
        for _ in range(3):
            await breaker.record(error)

        assert breaker.state == CircuitBreakerState.OPEN
        assert breaker.is_open() is True

    @pytest.mark.asyncio
    async def test_open_blocks_requests(self, breaker):
        error = LLMErrorInfo(kind=LLMErrorKind.TIMEOUT, message="timeout")
        for _ in range(3):
            await breaker.record(error)

        assert await breaker.allow_request() is False

    @pytest.mark.asyncio
    async def test_non_retryable_errors_ignored(self, breaker):
        auth_error = LLMErrorInfo(kind=LLMErrorKind.AUTH, message="401")
        for _ in range(10):
            await breaker.record(auth_error)

        assert breaker.state == CircuitBreakerState.CLOSED

    @pytest.mark.asyncio
    async def test_server_errors_count(self, breaker):
        error = LLMErrorInfo(kind=LLMErrorKind.SERVER, message="500")
        for _ in range(3):
            await breaker.record(error)

        assert breaker.state == CircuitBreakerState.OPEN

    @pytest.mark.asyncio
    async def test_half_open_after_cooldown(self, breaker):
        error = LLMErrorInfo(kind=LLMErrorKind.TIMEOUT, message="timeout")
        for _ in range(3):
            await breaker.record(error)

        assert breaker.state == CircuitBreakerState.OPEN

        # Wait for cooldown
        await asyncio.sleep(1.1)

        # allow_request transitions to HALF_OPEN
        assert await breaker.allow_request() is True
        assert breaker.state == CircuitBreakerState.HALF_OPEN

    @pytest.mark.asyncio
    async def test_half_open_success_closes(self, breaker):
        error = LLMErrorInfo(kind=LLMErrorKind.TIMEOUT, message="timeout")
        for _ in range(3):
            await breaker.record(error)

        await asyncio.sleep(1.1)
        await breaker.allow_request()
        assert breaker.state == CircuitBreakerState.HALF_OPEN

        await breaker.record_success()
        assert breaker.state == CircuitBreakerState.CLOSED

    @pytest.mark.asyncio
    async def test_half_open_failure_reopens(self, breaker):
        error = LLMErrorInfo(kind=LLMErrorKind.TIMEOUT, message="timeout")
        for _ in range(3):
            await breaker.record(error)

        await asyncio.sleep(1.1)
        await breaker.allow_request()
        assert breaker.state == CircuitBreakerState.HALF_OPEN

        await breaker.record_failure()
        assert breaker.state == CircuitBreakerState.OPEN

    @pytest.mark.asyncio
    async def test_success_in_closed_decrements_failure_count(self, breaker):
        error = LLMErrorInfo(kind=LLMErrorKind.TIMEOUT, message="timeout")
        await breaker.record(error)
        await breaker.record(error)
        # 2 failures, not yet open

        await breaker.record_success()
        # Should decrement to 1

        await breaker.record(error)
        # Still only 2, not open
        assert breaker.state == CircuitBreakerState.CLOSED

    @pytest.mark.asyncio
    async def test_record_failure_method(self, breaker):
        for _ in range(3):
            await breaker.record_failure()

        assert breaker.state == CircuitBreakerState.OPEN

    @pytest.mark.asyncio
    async def test_snapshot(self, breaker):
        snap = breaker.to_snapshot()
        assert snap["name"] == "test"
        assert snap["state"] == "closed"
        assert snap["failure_threshold"] == 3
        assert snap["cooldown_seconds"] == 1.0
        assert snap["enabled"] is True


class TestCircuitBreakerPolicy:
    """Policy defaults and construction."""

    def test_defaults(self):
        policy = CircuitBreakerPolicy()
        assert policy.enabled is True
        assert policy.failure_threshold == 3
        assert policy.cooldown_seconds == 120.0

    def test_custom_values(self):
        policy = CircuitBreakerPolicy(
            enabled=False,
            failure_threshold=5,
            cooldown_seconds=60.0,
        )
        assert policy.enabled is False
        assert policy.failure_threshold == 5
        assert policy.cooldown_seconds == 60.0
