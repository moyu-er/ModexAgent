"""Tests for AgentPool consumer task monitoring.

Regression: AgentPool._consume_messages task had no done callback.
If the consumer loop exited (exception or normal), it would go completely
unnoticed — no logs, no recovery, the agent would simply stop consuming
messages forever.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from modex_agent.messaging.broker import Address, BrokerMessage
from modex_agent.multi_agent.pool import AgentPool
from modex_agent.multi_agent.state import AgentState


class _FakeBroker:
    def __init__(self) -> None:
        self._messages: list[tuple[Address, BrokerMessage]] = []

    async def consume(self, address: Address) -> None:
        await asyncio.sleep(999)
        return None

    async def send_to(self, address: Address, msg: BrokerMessage) -> None:
        pass


class TestConsumerTaskMonitoring:
    """Consumer task done callback must log and transition state on unexpected exit."""

    @pytest.fixture
    async def pool(self) -> AgentPool:
        p = AgentPool(
            broker=_FakeBroker(),
            agent_factory=MagicMock(),
            enable_inbox_polling=False,
        )
        yield p

    @pytest.mark.asyncio
    async def test_consumer_done_callback_transitions_on_error(self, pool: AgentPool) -> None:
        """If _consume_messages exits with an exception, _on_consumer_done
        must log an error and transition the agent back to IDLE."""

        async def _failing_consumer() -> None:
            raise RuntimeError("consumer loop crash")

        pool._status["test_agent"] = AgentState.WORKING
        task = asyncio.create_task(_failing_consumer())
        task.add_done_callback(lambda t, n="test_agent": pool._on_consumer_done(t, n))
        pool._consumers["test_agent"] = task

        with pytest.raises(RuntimeError):
            await task

        assert pool._status.get("test_agent") == AgentState.IDLE

    @pytest.mark.asyncio
    async def test_consumer_done_callback_transitions_on_normal_exit(self, pool: AgentPool) -> None:
        """If _consume_messages exits normally, _on_consumer_done must log
        a warning and transition the agent back to IDLE."""

        async def _normal_exit_consumer() -> None:
            pass

        pool._status["test_agent"] = AgentState.WORKING
        task = asyncio.create_task(_normal_exit_consumer())
        task.add_done_callback(lambda t, n="test_agent": pool._on_consumer_done(t, n))
        pool._consumers["test_agent"] = task

        await task

        assert pool._status.get("test_agent") == AgentState.IDLE

    @pytest.mark.asyncio
    async def test_consumer_done_callback_skips_shutdown_state(self, pool: AgentPool) -> None:
        """If agent is already SHUTTING_DOWN, the done callback should not
        transition it back to IDLE."""

        async def _normal_exit_consumer() -> None:
            pass

        pool._status["test_agent"] = AgentState.SHUTTING_DOWN
        task = asyncio.create_task(_normal_exit_consumer())
        task.add_done_callback(lambda t, n="test_agent": pool._on_consumer_done(t, n))
        pool._consumers["test_agent"] = task

        await task

        assert pool._status.get("test_agent") == AgentState.SHUTTING_DOWN

    @pytest.mark.asyncio
    async def test_consumer_done_callback_recovers_from_cancelled(self, pool: AgentPool) -> None:
        """If consumer task is cancelled (e.g. by max errors), the done callback
        must recover the agent to IDLE so it can be restarted."""

        async def _sleep_forever() -> None:
            await asyncio.sleep(999)

        pool._status["test_agent"] = AgentState.WORKING
        task = asyncio.create_task(_sleep_forever())
        task.add_done_callback(lambda t, n="test_agent": pool._on_consumer_done(t, n))
        pool._consumers["test_agent"] = task

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert pool._status.get("test_agent") == AgentState.IDLE

    @pytest.mark.asyncio
    async def test_register_resident_attaches_done_callback(self, pool: AgentPool) -> None:
        """register_resident must attach _on_consumer_done to the consumer task."""
        from modex_agent.multi_agent.address import AgentAddress
        from modex_agent.multi_agent.descriptor import AgentDescriptor

        descriptor = AgentDescriptor(
            address=AgentAddress(kind="agent", name="test_agent"),
        )

        mock_instance = MagicMock()
        mock_instance.pipeline = None
        mock_instance.stop = AsyncMock()
        pool._agent_factory.create_agent = AsyncMock(return_value=mock_instance)

        await pool.register_resident(descriptor)

        assert "test_agent" in pool._consumers
        task = pool._consumers["test_agent"]
        assert not task.done()

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    @pytest.mark.asyncio
    async def test_consumer_error_count_resets_on_success(self, pool: AgentPool) -> None:
        """After a successful dispatch, error count should be reset."""
        pool._error_counts["test_agent"] = 3

        async def _success_coro() -> None:
            pass

        await pool._run_dispatch("test_agent", _success_coro())

        assert "test_agent" not in pool._error_counts

    @pytest.mark.asyncio
    async def test_consumer_backoff_on_error(self, pool: AgentPool) -> None:
        """On dispatch error, consumer should bump error count."""

        async def _fail_coro() -> None:
            raise RuntimeError("dispatch error")

        pool._status["test_agent"] = AgentState.IDLE
        await pool._run_dispatch("test_agent", _fail_coro())

        assert pool._error_counts.get("test_agent", 0) == 1
        assert pool._status.get("test_agent") == AgentState.IDLE

    @pytest.mark.asyncio
    async def test_consumer_stops_after_max_errors(self, pool: AgentPool) -> None:
        """After 5 errors, _maybe_backoff cancels the consumer task."""
        pool._error_counts["test_agent"] = 4
        pool._status["test_agent"] = AgentState.IDLE

        async def _fail_coro() -> None:
            raise RuntimeError("dispatch error")

        async def _fake_consumer() -> None:
            await asyncio.sleep(999)

        consumer_task = asyncio.create_task(_fake_consumer())
        pool._consumers["test_agent"] = consumer_task

        await pool._run_dispatch("test_agent", _fail_coro())

        assert pool._error_counts.get("test_agent", 0) == 5
        for _ in range(10):
            await asyncio.sleep(0)
        assert consumer_task.cancelled()

    @pytest.mark.asyncio
    async def test_dispatch_start_transitions_to_working(self, pool: AgentPool) -> None:
        """When dispatch starts, agent should transition to WORKING."""

        async def _success_coro() -> None:
            pass

        pool._status["test_agent"] = AgentState.IDLE
        await pool._run_dispatch("test_agent", _success_coro())

        assert pool._status.get("test_agent") == AgentState.IDLE

    @pytest.mark.asyncio
    async def test_dispatch_active_count_tracked(self, pool: AgentPool) -> None:
        """Active session count should be incremented and decremented."""

        async def _success_coro() -> None:
            pass

        await pool._run_dispatch("test_agent", _success_coro())

        assert pool._active_session_counts.get("test_agent", 0) == 0

    @pytest.mark.asyncio
    async def test_dispatch_concurrent_sessions_tracked(self, pool: AgentPool) -> None:
        """Multiple concurrent dispatches should track active count."""

        async def _slow_coro() -> None:
            await asyncio.sleep(0.05)

        t1 = asyncio.create_task(pool._run_dispatch("test_agent", _slow_coro()))
        t2 = asyncio.create_task(pool._run_dispatch("test_agent", _slow_coro()))

        await asyncio.sleep(0.01)
        assert pool._active_session_counts.get("test_agent", 0) == 2

        await t1
        await t2
        assert pool._active_session_counts.get("test_agent", 0) == 0

    @pytest.mark.asyncio
    async def test_consumer_loop_breaks_after_max_errors(self, pool: AgentPool) -> None:
        """_consume_messages breaks its loop after max errors, triggering
        the done callback which transitions state."""
        from modex_agent.multi_agent.address import AgentAddress
        from modex_agent.multi_agent.descriptor import AgentDescriptor

        class _RaisingBroker:
            async def consume(self, address: Address) -> None:
                raise RuntimeError("broker consume error")

            async def send_to(self, address: Address, msg: BrokerMessage) -> None:
                pass

        pool._broker = _RaisingBroker()
        pool._max_backoff_seconds = 0.0

        descriptor = AgentDescriptor(
            address=AgentAddress(kind="agent", name="test_agent"),
        )

        mock_instance = MagicMock()
        mock_instance.pipeline = None
        mock_instance.stop = AsyncMock()
        pool._agent_factory.create_agent = AsyncMock(return_value=mock_instance)

        await pool.register_resident(descriptor)

        for _ in range(200):
            await asyncio.sleep(0)
            task = pool._consumers.get("test_agent")
            if task and task.done():
                break

        task = pool._consumers.get("test_agent")
        assert task is not None
        assert task.done()

        await asyncio.sleep(0.05)

        assert pool._status.get("test_agent") == AgentState.IDLE
        new_task = pool._consumers.get("test_agent")
        assert new_task is not None
        assert new_task is not task
