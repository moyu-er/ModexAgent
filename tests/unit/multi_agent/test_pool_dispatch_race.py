"""Tests for AgentPool dispatch race condition and retry limit fixes."""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from modex_agent.core.llm_struct import RuntimeSafetyPolicy, TurnTimeoutPolicy
from modex_agent.multi_agent.pool import AgentPool
from modex_agent.multi_agent.state import AgentState


class _FakeBroker:
    async def consume(self, address):
        return None

    async def send_to(self, address, msg):
        pass


class TestDispatchRaceCondition:
    """Regression tests for dispatch race condition where _active_session_counts
    is updated based on a stale local variable, causing incorrect state transitions.
    """

    @pytest.fixture
    async def pool(self):
        safety = RuntimeSafetyPolicy(
            turn=TurnTimeoutPolicy(dispatch_timeout_seconds=0.1)
        )
        p = AgentPool(
            broker=_FakeBroker(),
            agent_factory=MagicMock(),
            enable_inbox_polling=False,
            safety=safety,
        )
        yield p
        await p.shutdown_all(timeout=0.1)

    @pytest.mark.asyncio
    async def test_concurrent_dispatch_counts_are_consistent(self, pool):
        """When two dispatches overlap, the active count must remain correct."""
        async def slow_coro():
            await asyncio.sleep(10)

        async def fast_coro():
            await asyncio.sleep(0.05)

        task1 = asyncio.create_task(pool._run_dispatch("main", slow_coro()))
        await asyncio.sleep(0.01)
        task2 = asyncio.create_task(pool._run_dispatch("main", fast_coro()))

        await asyncio.gather(task1, task2, return_exceptions=True)

        assert pool._active_session_counts.get("main", 0) == 0
        assert pool.get_status("main") == AgentState.IDLE

    @pytest.mark.asyncio
    async def test_dispatch_timeout_backoff_is_capped(self, pool):
        """Backoff time should be capped at _max_backoff_seconds."""
        pool._max_error_retries = 3
        pool._max_backoff_seconds = 0.5

        async def slow_coro():
            await asyncio.sleep(10)

        for _ in range(5):
            await pool._run_dispatch("main", slow_coro())

        assert pool._error_counts.get("main", 0) == 3

    @pytest.mark.asyncio
    async def test_dispatch_error_count_limited(self, pool):
        """Error count should be limited by _max_error_retries."""
        pool._max_error_retries = 2
        pool._max_backoff_seconds = 0.1

        async def failing_coro():
            raise ValueError("test error")

        for _ in range(5):
            await pool._run_dispatch("main", failing_coro())

        assert pool._error_counts.get("main", 0) == pool._max_error_retries
