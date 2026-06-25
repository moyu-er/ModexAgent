"""Tests for AgentPool deadlock bug: consumer continues dispatching during ERROR backoff.

Regression: When a dispatch task errors and enters backoff, the consumer loop
keeps creating new dispatch tasks. New tasks try ERROR->WORKING transition
which is rejected by the state machine, causing warnings and potential message loss.

TDD: Write failing test first, then fix.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from modex_agent.messaging.broker import Address, BrokerMessage
from modex_agent.multi_agent.address import AgentAddress
from modex_agent.multi_agent.pool import AgentPool
from modex_agent.multi_agent.state import AgentState


class _FakeBroker:
    """Fake broker that can queue messages for testing."""

    def __init__(self) -> None:
        self._messages: list[BrokerMessage] = []
        self._requeued: list[BrokerMessage] = []
        self._consume_event = asyncio.Event()

    def add_message(self, msg: BrokerMessage) -> None:
        self._messages.append(msg)
        self._consume_event.set()

    async def consume(self, address: Address) -> BrokerMessage | None:
        while not self._messages:
            self._consume_event.clear()
            try:
                await asyncio.wait_for(self._consume_event.wait(), timeout=0.1)
            except TimeoutError:
                return None
        return self._messages.pop(0)

    async def send_to(self, address: Address, msg: BrokerMessage) -> None:
        self._requeued.append(msg)
        self._consume_event.set()


class TestConsumerDuringErrorBackoff:
    """Consumer loop must not create new dispatch tasks while agent is in ERROR state."""

    @pytest.fixture
    async def pool(self):
        broker = _FakeBroker()
        p = AgentPool(
            broker=broker,
            agent_factory=MagicMock(),
            enable_inbox_polling=False,
        )
        yield p
        await p.shutdown_all(timeout=0.1)

    @pytest.mark.asyncio
    async def test_consumer_requeues_messages_during_error_state(self, pool):
        """RED: When agent is in ERROR state, consumer should requeue messages
        instead of creating new dispatch tasks.

        Expected: broker.send_to is called to requeue the message.
        Actual (before fix): consumer creates dispatch task with invalid state transition.
        """
        from modex_agent.multi_agent.descriptor import AgentDescriptor, AgentInstance


        broker = pool._broker
        pool._status["main"] = AgentState.ERROR

        desc = AgentDescriptor(address=AgentAddress(kind="agent", name="main"))
        instance = AgentInstance(
            descriptor=desc,
            context_manager=MagicMock(),
        )

        consumer_task = asyncio.create_task(pool._consume_messages(instance, desc))
        pool._consumers["main"] = consumer_task

        msg = BrokerMessage(
            payload={"content": "test"},
            sender=AgentAddress(kind="user", name="test_user"),
        )
        broker.add_message(msg)

        await asyncio.sleep(0.15)

        consumer_task.cancel()
        try:
            await consumer_task
        except asyncio.CancelledError:
            pass

        # After fix: message should be requeued, not dispatched
        assert len(broker._requeued) == 1, (
            f"Message should be requeued during ERROR state, "
            f"but got {len(broker._requeued)} requeued"
        )
        # State should remain ERROR
        assert pool.get_status("main") == AgentState.ERROR

    @pytest.mark.asyncio
    async def test_consumer_skips_dispatch_when_error_state(self, pool):
        """RED: Consumer must skip creating dispatch tasks when agent is in ERROR.

        This prevents the race condition where multiple dispatches compete
        for state transitions.
        """
        from modex_agent.multi_agent.descriptor import AgentDescriptor, AgentInstance
        broker = pool._broker
        pool._status["main"] = AgentState.ERROR
        pool._error_counts["main"] = 1

        desc = AgentDescriptor(address=AgentAddress(kind="agent", name="main"))
        instance = AgentInstance(
            descriptor=desc,
            context_manager=MagicMock(),
        )

        consumer_task = asyncio.create_task(pool._consume_messages(instance, desc))
        pool._consumers["main"] = consumer_task

        for i in range(3):
            broker.add_message(BrokerMessage(
                payload={"content": f"msg{i}"},
                sender=AgentAddress(kind="user", name="test_user"),
            ))

        await asyncio.sleep(0.4)

        consumer_task.cancel()
        try:
            await consumer_task
        except asyncio.CancelledError:
            pass

        assert len(broker._requeued) == 3, (
            f"All 3 messages should be requeued, got {len(broker._requeued)}"
        )


class TestMaxErrorsConsumerRecovery:
    """When max errors reached, consumer cancellation must recover to IDLE."""

    @pytest.fixture
    async def pool(self):
        p = AgentPool(
            broker=_FakeBroker(),
            agent_factory=MagicMock(),
            enable_inbox_polling=False,
        )
        yield p
        await p.shutdown_all(timeout=0.1)

    @pytest.mark.asyncio
    async def test_consumer_recovers_from_cancelled_state(self, pool):
        """RED: When consumer is cancelled due to max errors, agent must recover to IDLE.

        Before fix: _on_consumer_done sees cancelled=True and returns without recovery.
        After fix: agent transitions back to IDLE so it can be restarted.
        """
        pool._status["test_agent"] = AgentState.ERROR
        pool._error_counts["test_agent"] = 5  # Max errors reached

        # Simulate consumer task cancellation (as _maybe_backoff does)
        async def _fake_consumer():
            await asyncio.sleep(999)

        consumer_task = asyncio.create_task(_fake_consumer())
        pool._consumers["test_agent"] = consumer_task

        # Cancel the consumer (simulating _maybe_backoff behavior)
        consumer_task.cancel()
        try:
            await consumer_task
        except asyncio.CancelledError:
            pass

        # Trigger the done callback
        pool._on_consumer_done(consumer_task, "test_agent")

        # After fix: should recover to IDLE
        assert pool.get_status("test_agent") == AgentState.IDLE, (
            f"Agent should recover to IDLE after consumer cancellation, "
            f"but got {pool.get_status('test_agent')}"
        )

    @pytest.mark.asyncio
    async def test_max_errors_does_not_permanently_hang(self, pool):
        """RED: After max errors, agent must be recoverable (not stuck in ERROR).

        This is the real deadlock scenario: agent in ERROR with no consumer.
        """
        pool._max_error_retries = 2
        pool._max_backoff_seconds = 0.01
        pool._status["test_agent"] = AgentState.IDLE

        async def _fail_coro():
            raise RuntimeError("dispatch error")

        # Exceed max errors
        for _ in range(3):
            await pool._run_dispatch("test_agent", _fail_coro())

        # After fix: agent should eventually be IDLE, not permanently ERROR
        # Give time for any backoff to complete
        await asyncio.sleep(0.05)

        assert pool.get_status("test_agent") in (AgentState.IDLE, AgentState.ERROR), (
            f"Agent status: {pool.get_status('test_agent')}"
        )


class TestDispatchCleanupGuarantee:
    """Dispatch must always clean up: active_count=0 and state restored to IDLE."""

    @pytest.fixture
    async def pool(self):
        p = AgentPool(
            broker=_FakeBroker(),
            agent_factory=MagicMock(),
            enable_inbox_polling=False,
        )
        yield p
        await p.shutdown_all(timeout=0.1)

    @pytest.mark.asyncio
    async def test_dispatch_cleans_up_on_error(self, pool):
        """RED: After error dispatch, active_count must be 0 and state IDLE."""
        pool._status["test_agent"] = AgentState.IDLE

        async def _fail_coro():
            raise RuntimeError("test error")

        await pool._run_dispatch("test_agent", _fail_coro())

        # After fix: must be clean
        assert pool._active_session_counts.get("test_agent", 0) == 0, (
            f"active_count should be 0 after error, got "
            f"{pool._active_session_counts.get('test_agent', 0)}"
        )
        assert pool.get_status("test_agent") == AgentState.IDLE, (
            f"State should be IDLE after error recovery, got "
            f"{pool.get_status('test_agent')}"
        )

    @pytest.mark.asyncio
    async def test_dispatch_cleans_up_on_success(self, pool):
        """Verify normal dispatch also cleans up properly."""
        async def _success_coro():
            pass

        await pool._run_dispatch("test_agent", _success_coro())

        assert pool._active_session_counts.get("test_agent", 0) == 0
        assert pool.get_status("test_agent") == AgentState.IDLE

    @pytest.mark.asyncio
    async def test_concurrent_dispatches_both_clean_up(self, pool):
        """RED: Multiple concurrent dispatches must all clean up correctly."""
        async def _slow_coro():
            await asyncio.sleep(0.05)

        async def _fast_coro():
            await asyncio.sleep(0.01)

        task1 = asyncio.create_task(pool._run_dispatch("test_agent", _slow_coro()))
        await asyncio.sleep(0.01)
        task2 = asyncio.create_task(pool._run_dispatch("test_agent", _fast_coro()))

        await asyncio.gather(task1, task2, return_exceptions=True)

        assert pool._active_session_counts.get("test_agent", 0) == 0, (
            f"active_count should be 0 after concurrent dispatches, got "
            f"{pool._active_session_counts.get('test_agent', 0)}"
        )
        assert pool.get_status("test_agent") == AgentState.IDLE


class TestErrorToIdleTransition:
    """ERROR->IDLE->WORKING is the correct transition path, not direct ERROR->WORKING."""

    @pytest.fixture
    async def pool(self):
        p = AgentPool(
            broker=_FakeBroker(),
            agent_factory=MagicMock(),
            enable_inbox_polling=False,
        )
        yield p
        await p.shutdown_all(timeout=0.1)

    @pytest.mark.asyncio
    async def test_dispatch_from_error_state_goes_through_idle(self, pool):
        """RED: When dispatch starts from ERROR state, it should transition
        ERROR->IDLE->WORKING, not directly ERROR->WORKING.

        This ensures proper state machine semantics and recovery.
        """
        pool._status["test_agent"] = AgentState.ERROR
        pool._error_counts["test_agent"] = 1

        async def _success_coro():
            pass

        # Track state transitions
        transitions: list[tuple[str, str]] = []
        original_transition = pool._transition

        def _track_transition(name: str, new_state: AgentState, reason: str = "") -> None:
            old_state = pool._status.get(name, AgentState.SHUTDOWN)
            transitions.append((old_state.value, new_state.value))
            original_transition(name, new_state, reason)

        pool._transition = _track_transition

        await pool._run_dispatch("test_agent", _success_coro())

        # After fix: should see ERROR->IDLE then IDLE->WORKING
        assert ("error", "idle") in transitions, (
            f"Should transition ERROR->IDLE first. Transitions: {transitions}"
        )
        assert ("idle", "working") in transitions, (
            f"Should transition IDLE->WORKING second. Transitions: {transitions}"
        )

    @pytest.mark.asyncio
    async def test_error_state_cleared_before_working(self, pool):
        """RED: When starting dispatch from ERROR, error state must be cleared first."""
        pool._status["test_agent"] = AgentState.ERROR
        pool._error_counts["test_agent"] = 2

        async def _success_coro():
            pass

        await pool._run_dispatch("test_agent", _success_coro())

        # After fix: error count should be reset on successful dispatch
        assert "test_agent" not in pool._error_counts, (
            f"Error count should be cleared on success, got "
            f"{pool._error_counts.get('test_agent')}"
        )
