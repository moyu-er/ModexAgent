"""Tests for DispatchDeadline — renewable dispatch timeout with hard ceiling."""
from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock

import pytest

from modex_agent.core.llm_struct import RuntimeSafetyPolicy, TurnTimeoutPolicy
from modex_agent.multi_agent.pool import AgentPool
from modex_agent.multi_agent.state import AgentState
from modex_agent.runtime.dispatch import DispatchDeadline, current_dispatch_deadline


class _FakeBroker:
    async def consume(self, address):
        return None

    async def send_to(self, address, msg):
        pass


class TestDispatchDeadlineUnit:
    """Pure unit tests for DispatchDeadline value object."""

    def test_is_expired_false_before_deadline(self):
        d = DispatchDeadline(initial_timeout=10.0)
        assert not d.is_expired

    def test_is_expired_true_after_deadline(self):
        d = DispatchDeadline(initial_timeout=0.0)
        time.sleep(0.01)
        assert d.is_expired

    def test_remaining_positive_before_deadline(self):
        d = DispatchDeadline(initial_timeout=1.0)
        assert d.remaining > 0.5

    def test_remaining_zero_after_deadline(self):
        d = DispatchDeadline(initial_timeout=0.0)
        time.sleep(0.01)
        assert d.remaining == 0.0

    def test_renew_extends_deadline(self):
        d = DispatchDeadline(initial_timeout=0.0, max_ahead_seconds=10.0)
        time.sleep(0.01)
        assert d.is_expired
        d.renew(0.5)
        assert not d.is_expired
        assert d.remaining > 0.3

    def test_renew_uses_seconds_param(self):
        d = DispatchDeadline(initial_timeout=0.0, max_ahead_seconds=10.0)
        time.sleep(0.01)
        d.renew(0.2)
        remaining_after_renew = d.remaining
        assert 0.1 < remaining_after_renew <= 0.21

    def test_renew_default_is_3_seconds(self):
        d = DispatchDeadline(initial_timeout=0.0, max_ahead_seconds=10.0)
        time.sleep(0.01)
        d.renew()
        assert d.remaining > 2.5

    def test_renew_never_shortens_deadline(self):
        d = DispatchDeadline(initial_timeout=0.3, max_ahead_seconds=10.0)
        time.sleep(0.05)
        remaining_before = d.remaining
        assert remaining_before > 0.2
        d.renew(0.1)
        remaining_after = d.remaining
        assert remaining_after >= remaining_before - 0.001

    def test_renew_capped_by_max_ahead(self):
        d = DispatchDeadline(initial_timeout=1.0, max_ahead_seconds=2.0)
        time.sleep(0.01)
        d.renew(100.0)
        assert d.remaining <= 2.0
        assert d.remaining > 1.5

    def test_renew_repeatedly_stays_within_ahead(self):
        d = DispatchDeadline(initial_timeout=0.5, max_ahead_seconds=1.0)
        for _ in range(20):
            d.renew(3.0)
            assert d.remaining <= 1.0


class TestDispatchDeadlineContextVar:
    """ContextVar injection and isolation."""

    def test_default_is_none(self):
        assert current_dispatch_deadline.get() is None

    def test_set_and_get(self):
        d = DispatchDeadline(initial_timeout=10.0)
        token = current_dispatch_deadline.set(d)
        assert current_dispatch_deadline.get() is d
        current_dispatch_deadline.reset(token)

    def test_reset_restores_none(self):
        d = DispatchDeadline(initial_timeout=10.0)
        token = current_dispatch_deadline.set(d)
        current_dispatch_deadline.reset(token)
        assert current_dispatch_deadline.get() is None


class TestPoolRenewableDispatch:
    """Pool._run_dispatch with renewable watchdog — timeout and renewal paths."""

    @pytest.fixture
    async def pool(self):
        safety = RuntimeSafetyPolicy(
            turn=TurnTimeoutPolicy(
                dispatch_timeout_seconds=0.15,
                agent_run_timeout_seconds=0.1,
            ),
        )
        p = AgentPool(
            broker=_FakeBroker(),
            agent_factory=MagicMock(),
            safety=safety,
        )
        p._max_backoff_seconds = 0.05
        yield p
        await p.shutdown_all(timeout=0.1)

    async def test_fast_coro_completes_normally(self, pool):
        async def fast():
            await asyncio.sleep(0.01)

        await pool._run_dispatch("main", fast())
        assert pool._error_counts.get("main", 0) == 0
        assert pool._active_session_counts.get("main", 0) == 0

    async def test_stuck_coro_times_out(self, pool):
        async def stuck():
            await asyncio.sleep(100)

        await pool._run_dispatch("main", stuck())
        assert pool._error_counts.get("main", 0) >= 1

    async def test_renewing_coro_never_times_out(self, pool):
        async def renewing_coro():
            deadline = current_dispatch_deadline.get()
            assert deadline is not None
            for _ in range(4):
                await asyncio.sleep(0.06)
                deadline.renew(0.1)

        await pool._run_dispatch("main", renewing_coro())
        assert pool._error_counts.get("main", 0) == 0
        assert pool.get_status("main") == AgentState.IDLE

    async def test_renew_then_stop_renewing_causes_timeout(self, pool):
        async def renew_then_stall():
            deadline = current_dispatch_deadline.get()
            assert deadline is not None
            for _ in range(2):
                await asyncio.sleep(0.04)
                deadline.renew(0.1)
            await asyncio.sleep(0.5)

        await pool._run_dispatch("main", renew_then_stall())
        assert pool._error_counts.get("main", 0) >= 1

    async def test_context_var_cleaned_up_after_dispatch(self, pool):
        async def noop():
            pass

        await pool._run_dispatch("main", noop())
        assert current_dispatch_deadline.get() is None

    async def test_context_var_cleaned_up_after_timeout(self, pool):
        async def stuck():
            await asyncio.sleep(100)

        await pool._run_dispatch("main", stuck())
        assert current_dispatch_deadline.get() is None

    async def test_renew_while_watchdog_sleeping_extends_timeout(self):
        safety = RuntimeSafetyPolicy(
            turn=TurnTimeoutPolicy(
                dispatch_timeout_seconds=0.5,
                agent_run_timeout_seconds=0.2,
            ),
        )
        p = AgentPool(
            broker=_FakeBroker(),
            agent_factory=MagicMock(),
            safety=safety,
        )
        p._max_backoff_seconds = 0.05

        async def renewing_coro():
            deadline = current_dispatch_deadline.get()
            assert deadline is not None
            await asyncio.sleep(0.1)
            deadline.renew(0.2)
            await asyncio.sleep(0.2)
            deadline.renew(0.2)
            await asyncio.sleep(0.15)
            deadline.renew(0.2)
            await asyncio.sleep(0.1)

        await p._run_dispatch("main", renewing_coro())
        assert p._error_counts.get("main", 0) == 0
        assert p.get_status("main") == AgentState.IDLE
        await p.shutdown_all(timeout=0.1)

    async def test_early_renew_does_not_steal_time(self):
        safety = RuntimeSafetyPolicy(
            turn=TurnTimeoutPolicy(
                dispatch_timeout_seconds=0.3,
                agent_run_timeout_seconds=0.1,
            ),
        )
        p = AgentPool(
            broker=_FakeBroker(),
            agent_factory=MagicMock(),
            safety=safety,
        )
        p._max_backoff_seconds = 0.05

        async def coro_with_early_renew():
            deadline = current_dispatch_deadline.get()
            assert deadline is not None
            await asyncio.sleep(0.05)
            deadline.renew(0.1)
            await asyncio.sleep(0.20)

        await p._run_dispatch("main", coro_with_early_renew())
        assert p._error_counts.get("main", 0) == 0
        assert p.get_status("main") == AgentState.IDLE
        await p.shutdown_all(timeout=0.1)

    async def test_no_watchdog_when_timeout_disabled(self):
        async def slow():
            await asyncio.sleep(0.05)

        safety = RuntimeSafetyPolicy(
            turn=TurnTimeoutPolicy(dispatch_timeout_seconds=0),
        )
        p = AgentPool(
            broker=_FakeBroker(),
            agent_factory=MagicMock(),
            safety=safety,
        )
        await p._run_dispatch("main", slow())
        assert current_dispatch_deadline.get() is None
        await p.shutdown_all(timeout=0.1)

    async def test_ceiling_caps_single_renew_burst(self, monkeypatch):
        """A single renew(huge) is capped to max_ahead, so if activity then
        stops, the watchdog kills the coro within max_ahead."""
        monkeypatch.setattr(DispatchDeadline, "DEFAULT_MAX_AHEAD_SECONDS", 0.3)

        safety = RuntimeSafetyPolicy(
            turn=TurnTimeoutPolicy(
                dispatch_timeout_seconds=0.1,
                agent_run_timeout_seconds=0.05,
            ),
        )
        p = AgentPool(
            broker=_FakeBroker(),
            agent_factory=MagicMock(),
            safety=safety,
        )
        p._max_backoff_seconds = 0.05

        async def burst_then_stall():
            deadline = current_dispatch_deadline.get()
            assert deadline is not None
            deadline.renew(999.0)  # capped to now+0.3
            await asyncio.sleep(1.0)  # stall well past 0.3

        await p._run_dispatch("main", burst_then_stall())
        assert p._error_counts.get("main", 0) >= 1
        await p.shutdown_all(timeout=0.1)
