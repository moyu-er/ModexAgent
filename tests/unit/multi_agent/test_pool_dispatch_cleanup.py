"""Dispatch task cleanup — external cancellation must cancel the inner dispatch_task.

When _run_dispatch is cancelled externally (not by the watchdog), the inner
dispatch_task created via ensure_future MUST also be cancelled. Otherwise it
leaks and continues consuming resources (LLM streams, etc.).
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from framework.core.llm_struct import RuntimeSafetyPolicy, TurnTimeoutPolicy
from framework.multi_agent.pool import AgentPool
from framework.multi_agent.state import AgentState


class _FakeBroker:
    async def consume(self, address):
        return None

    async def send_to(self, address, msg):
        pass


class TestDispatchCleanupOnExternalCancellation:

    @pytest.fixture
    async def pool(self):
        safety = RuntimeSafetyPolicy(
            turn=TurnTimeoutPolicy(dispatch_timeout_seconds=60),
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
    async def test_dispatch_task_cancelled_on_external_cancel(self, pool):
        """When _run_dispatch is cancelled externally, inner dispatch_task must
        be cancelled too, not left running in the background."""
        inner_cancelled = False

        async def long_running_coro():
            nonlocal inner_cancelled
            try:
                await asyncio.sleep(100)
            except asyncio.CancelledError:
                inner_cancelled = True
                raise

        outer_task = asyncio.create_task(
            pool._run_dispatch("main", long_running_coro()),
        )
        # Give the dispatch time to start the inner task
        await asyncio.sleep(0.05)

        # Cancel externally (simulating pipeline shutdown or user abort)
        outer_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await outer_task

        assert inner_cancelled, (
            "dispatch_task was not cancelled when _run_dispatch was "
            "cancelled externally — resource leak"
        )

    @pytest.mark.asyncio
    async def test_active_count_reset_after_external_cancel(self, pool):
        """After external cancellation, active session count must be correct."""
        async def long_running_coro():
            await asyncio.sleep(100)

        outer_task = asyncio.create_task(
            pool._run_dispatch("main", long_running_coro()),
        )
        await asyncio.sleep(0.05)

        outer_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await outer_task

        assert pool._active_session_counts.get("main", 0) == 0

    @pytest.mark.asyncio
    async def test_subsequent_dispatch_works_after_external_cancel(self, pool):
        """After external cancellation, a subsequent dispatch must succeed."""
        async def long_running_coro():
            await asyncio.sleep(100)

        async def quick_coro():
            pass

        # First dispatch: cancelled externally
        outer = asyncio.create_task(
            pool._run_dispatch("main", long_running_coro()),
        )
        await asyncio.sleep(0.05)
        outer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await outer

        # Second dispatch: should work fine
        await pool._run_dispatch("main", quick_coro())
        assert pool._active_session_counts.get("main", 0) == 0
        assert pool.get_status("main") == AgentState.IDLE
