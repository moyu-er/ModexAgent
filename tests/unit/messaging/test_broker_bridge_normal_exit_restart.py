"""Tests for BrokerBridgeService resilience fixes.

Fix 1: _bridge_input outer while True loop catches exceptions and retries,
preventing the bridge task from exiting when adapter.receive() fails.

Fix 2: _bridge_done_callback restarts on normal completion (defense-in-depth),
in case a bridge task somehow exits without exception.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from modex_agent.core.session_id import SessionInfo
from modex_agent.core.types import InputMessage
from modex_agent.messaging.broker import Address, BrokerMessage
from modex_agent.messaging.broker_bridge import BrokerBridgeService


class _FakeBroker:
    def __init__(self) -> None:
        self._started = False
        self._messages: list[Any] = []

    async def start(self) -> None:
        self._started = True

    async def stop(self) -> None:
        self._started = False

    async def register_consumer(self, address: Address) -> None:
        pass

    async def unregister_consumer(self, address: Address) -> None:
        pass

    async def send_to(self, address: Address, msg: BrokerMessage) -> None:
        self._messages.append((address, msg))

    async def publish(self, topic: str, msg: BrokerMessage) -> None:
        self._messages.append((topic, msg))

    async def consume_stream(self, address: Address) -> None:
        if not self._started:
            return
        while True:
            await asyncio.sleep(999)

    async def subscribe(self, topics: list[str]) -> None:
        if not self._started:
            return
        while True:
            await asyncio.sleep(999)


class _FailingAdapter:
    """Adapter that raises on every receive() call."""

    def __init__(self, name: str = "failing") -> None:
        self._name = name
        self._started = False
        self._calls = 0

    @property
    def name(self) -> str:
        return self._name

    async def start(self) -> None:
        self._started = True

    async def stop(self) -> None:
        self._started = False

    def receive(self) -> AsyncIterator[InputMessage]:
        self._calls += 1

        async def _gen() -> AsyncIterator[InputMessage]:
            raise RuntimeError("adapter receive failure")
            yield InputMessage(content="", session=SessionInfo.from_str("test", default_agent_name="main"), source="test")  # noqa: UNREACHABLE

        return _gen()


class _RecoveringAdapter:
    """Adapter that fails once then yields one message then blocks."""

    def __init__(self) -> None:
        self._attempt = 0
        self._name = "recovering"
        self._started = False

    @property
    def name(self) -> str:
        return self._name

    async def start(self) -> None:
        self._started = True

    async def stop(self) -> None:
        self._started = False

    def receive(self) -> AsyncIterator[InputMessage]:
        self._attempt += 1
        if self._attempt == 1:

            async def _fail() -> AsyncIterator[InputMessage]:
                raise RuntimeError("first attempt fails")
                yield  # noqa: PIE786

            return _fail()

        async def _gen() -> AsyncIterator[InputMessage]:
            yield InputMessage(
                content="recovered",
                session=SessionInfo.from_str("test", default_agent_name="main"),
                source="test",
            )
            await asyncio.sleep(999)

        return _gen()


class _ExhaustingThenBlockingAdapter:
    """Adapter that yields N messages on first call, then blocks forever."""

    def __init__(self, name: str = "exhausting", message_count: int = 1) -> None:
        self._name = name
        self._message_count = message_count
        self._yielded = 0
        self._started = False
        self._calls = 0

    @property
    def name(self) -> str:
        return self._name

    async def start(self) -> None:
        self._started = True

    async def stop(self) -> None:
        self._started = False

    def receive(self) -> AsyncIterator[InputMessage]:
        self._calls += 1
        if self._calls > 1:

            async def _block() -> AsyncIterator[InputMessage]:
                await asyncio.sleep(999)
                yield InputMessage(content="", session=SessionInfo.from_str("test", default_agent_name="main"), source="test")

            return _block()

        async def _gen() -> AsyncIterator[InputMessage]:
            while self._yielded < self._message_count:
                self._yielded += 1
                yield InputMessage(
                    content=f"msg-{self._yielded}",
                    session=SessionInfo.from_str("test", default_agent_name="main"),
                    source="test",
                )

        return _gen()


class TestBridgeInputRetryLoop:
    """_bridge_input outer while True loop prevents bridge task death."""

    @pytest.mark.asyncio
    async def test_bridge_input_retries_after_adapter_exception(self) -> None:
        """When adapter.receive() raises, _bridge_input should not exit.
        The while True loop catches the exception and retries."""
        broker = _FakeBroker()
        adapter = _FailingAdapter("in1")
        service = BrokerBridgeService(
            broker=broker,
            input_bindings={adapter: Address(kind="test", name="addr1")},
            restart_on_failure=False,
        )

        await service.start()
        task = service._tasks[0]

        await asyncio.sleep(1.1)

        assert not task.done()
        assert adapter._calls >= 2
        await service.stop()

    @pytest.mark.asyncio
    async def test_bridge_input_forwards_messages_after_retry(self) -> None:
        """After an initial failure, if a subsequent adapter.receive() succeeds,
        messages should be forwarded normally."""
        broker = _FakeBroker()
        adapter = _RecoveringAdapter()
        service = BrokerBridgeService(
            broker=broker,
            input_bindings={adapter: Address(kind="test", name="addr1")},
            restart_on_failure=False,
        )

        await service.start()
        await asyncio.sleep(1.1)

        assert adapter._attempt >= 2
        assert len(broker._messages) == 1
        assert broker._messages[0][1].payload["content"] == "recovered"
        await service.stop()

    @pytest.mark.asyncio
    async def test_bridge_input_does_not_exit_on_generator_exhaustion(self) -> None:
        """When adapter.receive() generator exhausts, _bridge_input should not exit.
        The while True loop calls receive() again, which blocks."""
        broker = _FakeBroker()
        adapter = _ExhaustingThenBlockingAdapter("in1", message_count=2)
        service = BrokerBridgeService(
            broker=broker,
            input_bindings={adapter: Address(kind="test", name="addr1")},
            restart_on_failure=False,
        )

        await service.start()
        task = service._tasks[0]

        await asyncio.sleep(0.1)

        assert len(broker._messages) == 2
        assert not task.done()
        assert adapter._calls >= 2
        await service.stop()

    @pytest.mark.asyncio
    async def test_bridge_input_respects_cancelled_error(self) -> None:
        """asyncio.CancelledError must propagate out of the retry loop
        so that stop() can cancel the task cleanly."""
        broker = _FakeBroker()

        class _CancellingAdapter:
            def __init__(self) -> None:
                self._name = "cancelling"
                self._started = False

            @property
            def name(self) -> str:
                return self._name

            async def start(self) -> None:
                self._started = True

            async def stop(self) -> None:
                self._started = False

            def receive(self) -> AsyncIterator[InputMessage]:
                async def _gen() -> AsyncIterator[InputMessage]:
                    await asyncio.sleep(999)
                    yield InputMessage(content="", session=SessionInfo.from_str("test", default_agent_name="main"), source="test")

                return _gen()

        adapter = _CancellingAdapter()
        service = BrokerBridgeService(
            broker=broker,
            input_bindings={adapter: Address(kind="test", name="addr1")},
            restart_on_failure=False,
        )

        await service.start()
        task = service._tasks[0]

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


class TestBridgeDoneCallbackDefenseInDepth:
    """_bridge_done_callback must restart even on normal completion."""

    @pytest.mark.asyncio
    async def test_restarts_on_normal_completion(self) -> None:
        """If a bridge task somehow completes normally (not via exception),
        the done callback should still schedule a restart."""
        broker = _FakeBroker()
        adapter = _ExhaustingThenBlockingAdapter("in1", message_count=1)
        service = BrokerBridgeService(
            broker=broker,
            input_bindings={adapter: Address(kind="test", name="addr1")},
            restart_on_failure=True,
            restart_max_retries=2,
            restart_backoff_seconds=0.001,
        )

        await service.start()
        assert len(service._tasks) == 1

        async def _noop() -> None:
            pass

        dummy_task = asyncio.create_task(_noop())
        await dummy_task
        service._bridge_done_callback(dummy_task, "input:in1")
        # Wait for the restart scheduling task to finish and be pruned.
        for _ in range(50):
            if len(service._tasks) == 2:
                break
            await asyncio.sleep(0.01)

        assert len(service._tasks) == 2
        await service.stop()

    @pytest.mark.asyncio
    async def test_no_restart_on_normal_completion_when_disabled(self) -> None:
        """If restart_on_failure is False, normal completion should not restart."""
        broker = _FakeBroker()
        service = BrokerBridgeService(
            broker=broker,
            input_bindings={},
            restart_on_failure=False,
        )

        async def _noop() -> None:
            pass

        dummy_task = asyncio.create_task(_noop())
        await dummy_task
        service._bridge_done_callback(dummy_task, "input:test")
        await asyncio.sleep(0.01)

        assert len(service._tasks) == 0
