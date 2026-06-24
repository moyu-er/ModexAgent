"""Tests for DispatchDeadline — renewable dispatch timeout mechanism."""
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
        d = DispatchDeadline(initial_timeout=10.0, extension=5.0)
        assert not d.is_expired

    def test_is_expired_true_after_deadline(self):
        d = DispatchDeadline(initial_timeout=0.0, extension=5.0)
        time.sleep(0.01)
        assert d.is_expired

    def test_remaining_positive_before_deadline(self):
        d = DispatchDeadline(initial_timeout=1.0, extension=5.0)
        assert d.remaining > 0.5

    def test_remaining_zero_after_deadline(self):
        d = DispatchDeadline(initial_timeout=0.0, extension=5.0)
        time.sleep(0.01)
        assert d.remaining == 0.0

    def test_renew_extends_deadline(self):
        d = DispatchDeadline(initial_timeout=0.0, extension=0.5)
        time.sleep(0.01)
        assert d.is_expired
        d.renew()
        assert not d.is_expired
        assert d.remaining > 0.3

    def test_renew_uses_extension_value(self):
        d = DispatchDeadline(initial_timeout=0.0, extension=0.2)
        time.sleep(0.01)
        d.renew()
        remaining_after_renew = d.remaining
        assert 0.1 < remaining_after_renew <= 0.2

    def test_renew_never_shortens_deadline(self):
        """ renew() must not push expires_at earlier than it already is.

        Scenario: initial_timeout=0.3s, extension=0.1s.
        At t=0.05s, remaining before renew = 0.25s.
        If renew() simply does `now + extension`, deadline becomes 0.15s —
        that is SHORTER than the original 0.3s.  This is the bug.
        """
        d = DispatchDeadline(initial_timeout=0.3, extension=0.1)
        time.sleep(0.05)
        remaining_before = d.remaining
        assert remaining_before > 0.2  # still > 0.2s left from original 0.3
        d.renew()
        remaining_after = d.remaining
        # renew() must extend (or keep), never shorten.
        # A tiny epsilon accounts for monotonic clock drift between the two
        # remaining queries.
        assert remaining_after >= remaining_before - 0.001


class TestDispatchDeadlineContextVar:
    """ContextVar injection and isolation."""

    def test_default_is_none(self):
        assert current_dispatch_deadline.get() is None

    def test_set_and_get(self):
        d = DispatchDeadline(initial_timeout=10.0, extension=5.0)
        token = current_dispatch_deadline.set(d)
        assert current_dispatch_deadline.get() is d
        current_dispatch_deadline.reset(token)

    def test_reset_restores_none(self):
        d = DispatchDeadline(initial_timeout=10.0, extension=5.0)
        token = current_dispatch_deadline.set(d)
        current_dispatch_deadline.reset(token)
        assert current_dispatch_deadline.get() is None


class _FakeBroker:
    async def consume(self, address):
        return None

    async def send_to(self, address, msg):
        pass


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
            enable_inbox_polling=False,
            safety=safety,
        )
        p._max_backoff_seconds = 0.05
        yield p
        await p.shutdown_all(timeout=0.1)

    @pytest.mark.asyncio
    async def test_fast_coro_completes_normally(self, pool):
        """Coro finishes well within deadline — no timeout."""

        async def fast():
            await asyncio.sleep(0.01)

        await pool._run_dispatch("main", fast())
        assert pool._error_counts.get("main", 0) == 0
        assert pool._active_session_counts.get("main", 0) == 0

    @pytest.mark.asyncio
    async def test_stuck_coro_times_out(self, pool):
        """Coro never returns → deadline expires → TimeoutError path."""

        async def stuck():
            await asyncio.sleep(100)

        await pool._run_dispatch("main", stuck())
        assert pool._error_counts.get("main", 0) >= 1

    @pytest.mark.asyncio
    async def test_renewing_coro_never_times_out(self, pool):
        """Coro keeps renewing deadline each iteration — runs past initial
        timeout without being killed."""

        async def renewing_coro():
            deadline = current_dispatch_deadline.get()
            assert deadline is not None
            for _ in range(4):
                await asyncio.sleep(0.06)
                deadline.renew()
            # 4 × 0.06 = 0.24s > 0.15s initial timeout, but we never expired

        await pool._run_dispatch("main", renewing_coro())
        assert pool._error_counts.get("main", 0) == 0
        assert pool.get_status("main") == AgentState.IDLE

    @pytest.mark.asyncio
    async def test_renew_then_stop_renewing_causes_timeout(self, pool):
        """Coro renews a few times, then stops → deadline eventually expires."""

        async def renew_then_stall():
            deadline = current_dispatch_deadline.get()
            assert deadline is not None
            for _ in range(2):
                await asyncio.sleep(0.04)
                deadline.renew()
            await asyncio.sleep(0.5)

        await pool._run_dispatch("main", renew_then_stall())
        assert pool._error_counts.get("main", 0) >= 1

    @pytest.mark.asyncio
    async def test_context_var_cleaned_up_after_dispatch(self, pool):
        """ContextVar must be reset to None after _run_dispatch finishes."""

        async def noop():
            pass

        await pool._run_dispatch("main", noop())
        assert current_dispatch_deadline.get() is None

    @pytest.mark.asyncio
    async def test_context_var_cleaned_up_after_timeout(self, pool):
        """ContextVar must be reset to None even when dispatch times out."""

        async def stuck():
            await asyncio.sleep(100)

        await pool._run_dispatch("main", stuck())
        assert current_dispatch_deadline.get() is None

    @pytest.mark.asyncio
    async def test_renew_while_watchdog_sleeping_extends_timeout(self):
        """Coro renews multiple times past initial timeout — completes safely.

        Verifies the full pool+watchdog+renew integration works when a
        dispatch runs longer than its initial_timeout but keeps renewing.
        """
        safety = RuntimeSafetyPolicy(
            turn=TurnTimeoutPolicy(
                dispatch_timeout_seconds=0.5,
                agent_run_timeout_seconds=0.2,
            ),
        )
        p = AgentPool(
            broker=_FakeBroker(),
            agent_factory=MagicMock(),
            enable_inbox_polling=False,
            safety=safety,
        )
        p._max_backoff_seconds = 0.05

        async def renewing_coro():
            deadline = current_dispatch_deadline.get()
            assert deadline is not None
            await asyncio.sleep(0.1)
            deadline.renew()          # t=0.1
            await asyncio.sleep(0.2)  # t=0.3
            deadline.renew()          # t=0.3
            await asyncio.sleep(0.15) # t=0.45
            deadline.renew()          # t=0.45
            await asyncio.sleep(0.1)  # t=0.55 — past initial 0.5s!

        await p._run_dispatch("main", renewing_coro())
        assert p._error_counts.get("main", 0) == 0
        assert p.get_status("main") == AgentState.IDLE
        await p.shutdown_all(timeout=0.1)

    @pytest.mark.asyncio
    async def test_early_renew_does_not_steal_time(self):
        """Early renew() must not shorten deadline → coro killed prematurely.

        With the old broken renew() (`_expires_at = now + extension`),
        an early renew at t=0.05 with extension=0.1 shortens deadline
        from 0.3 → 0.15.  The coro then gets killed at t=0.3 even though
        total elapsed is only 0.25s (well within original 0.3s budget).

        With max-based renew, deadline stays at 0.3 and the coro survives.
        """
        safety = RuntimeSafetyPolicy(
            turn=TurnTimeoutPolicy(
                dispatch_timeout_seconds=0.3,
                agent_run_timeout_seconds=0.1,
            ),
        )
        p = AgentPool(
            broker=_FakeBroker(),
            agent_factory=MagicMock(),
            enable_inbox_polling=False,
            safety=safety,
        )
        p._max_backoff_seconds = 0.05

        async def coro_with_early_renew():
            deadline = current_dispatch_deadline.get()
            assert deadline is not None
            await asyncio.sleep(0.05)   # quick first iteration
            deadline.renew()            # t=0.05
            await asyncio.sleep(0.20)   # second iteration takes longer

        await p._run_dispatch("main", coro_with_early_renew())
        assert p._error_counts.get("main", 0) == 0
        assert p.get_status("main") == AgentState.IDLE
        await p.shutdown_all(timeout=0.1)

    @pytest.mark.asyncio
    async def test_no_watchdog_when_timeout_disabled(self):
        """dispatch_timeout=0 skips watchdog entirely, ContextVar stays None."""

        async def slow():
            await asyncio.sleep(0.05)

        safety = RuntimeSafetyPolicy(
            turn=TurnTimeoutPolicy(dispatch_timeout_seconds=0),
        )
        p = AgentPool(
            broker=_FakeBroker(),
            agent_factory=MagicMock(),
            enable_inbox_polling=False,
            safety=safety,
        )
        await p._run_dispatch("main", slow())
        assert current_dispatch_deadline.get() is None
        await p.shutdown_all(timeout=0.1)
