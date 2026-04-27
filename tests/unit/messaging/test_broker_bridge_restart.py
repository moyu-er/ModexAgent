"""Tests for BrokerBridgeService restart_on_failure (P1 Step 15.2)."""

import asyncio

import pytest

from framework.core.types import InputMessage
from framework.messaging.broker import Address
from framework.messaging.broker_bridge import BrokerBridgeService


class FakeBroker:
    """Minimal fake broker for bridge tests."""

    def __init__(self):
        self._started = False
        self._messages: list = []

    async def start(self):
        self._started = True

    async def stop(self):
        self._started = False

    async def register_consumer(self, address):
        pass

    async def unregister_consumer(self, address):
        pass

    async def send_to(self, address, msg):
        self._messages.append((address, msg))

    async def publish(self, topic, msg):
        self._messages.append((topic, msg))

    async def consume_stream(self, address):
        if not self._started:
            return
        while True:
            await asyncio.sleep(999)

    async def subscribe(self, topics):
        if not self._started:
            return
        while True:
            await asyncio.sleep(999)


class FakeInputAdapter:
    def __init__(self, name="fake_in"):
        self._name = name
        self._started = False

    @property
    def name(self):
        return self._name

    async def start(self):
        self._started = True

    async def stop(self):
        self._started = False

    def receive(self):
        async def _gen():
            while True:
                await asyncio.sleep(999)
                yield InputMessage(content="", session_id="test", source="test")
        return _gen()


async def _fail_immediately():
    raise RuntimeError("bridge crash")


class TestBridgeRestart:
    """Bridge task restart with backoff tests."""

    @pytest.mark.asyncio
    async def test_restart_on_failure_creates_new_task(self):
        broker = FakeBroker()
        adapter = FakeInputAdapter("in1")
        service = BrokerBridgeService(
            broker=broker,
            input_bindings={adapter: Address(kind="test", name="addr1")},
            restart_on_failure=True,
            restart_max_retries=2,
            restart_backoff_seconds=0.05,
        )

        await service.start()
        assert len(service._tasks) == 1
        original_task = service._tasks[0]

        # Cancel the running task and inject a failing task
        original_task.cancel()
        try:
            await original_task
        except asyncio.CancelledError:
            pass

        fail_task = asyncio.create_task(_fail_immediately())
        fail_task.add_done_callback(
            lambda t, n=f"input:{adapter.name}": service._bridge_done_callback(t, n)
        )
        service._tasks[0] = fail_task
        try:
            await fail_task
        except RuntimeError:
            pass

        # Wait for restart
        await asyncio.sleep(0.15)

        # A new task should have been created
        assert len(service._tasks) == 2
        assert service._tasks[1] is not original_task
        await service.stop()

    @pytest.mark.asyncio
    async def test_no_restart_when_disabled(self):
        broker = FakeBroker()
        adapter = FakeInputAdapter("in1")
        service = BrokerBridgeService(
            broker=broker,
            input_bindings={adapter: Address(kind="test", name="addr1")},
            restart_on_failure=False,
        )

        await service.start()
        assert len(service._tasks) == 1
        original_task = service._tasks[0]

        original_task.cancel()
        try:
            await original_task
        except asyncio.CancelledError:
            pass

        fail_task = asyncio.create_task(_fail_immediately())
        fail_task.add_done_callback(
            lambda t, n=f"input:{adapter.name}": service._bridge_done_callback(t, n)
        )
        service._tasks[0] = fail_task
        try:
            await fail_task
        except RuntimeError:
            pass

        await asyncio.sleep(0.05)

        assert len(service._tasks) == 1
        await service.stop()

    @pytest.mark.asyncio
    async def test_max_restarts_respected(self):
        broker = FakeBroker()
        adapter = FakeInputAdapter("in1")
        service = BrokerBridgeService(
            broker=broker,
            input_bindings={adapter: Address(kind="test", name="addr1")},
            restart_on_failure=True,
            restart_max_retries=1,
            restart_backoff_seconds=0.01,
        )

        await service.start()
        task_count_after_start = len(service._tasks)

        # Inject a failing task
        for i, task in enumerate(list(service._tasks)):
            task.cancel()
            try:
                await task
            except (Exception, asyncio.CancelledError):
                pass
            fail_task = asyncio.create_task(_fail_immediately())
            fail_task.add_done_callback(
                lambda t, n=f"input:{adapter.name}": service._bridge_done_callback(t, n)
            )
            service._tasks[i] = fail_task
            try:
                await fail_task
            except RuntimeError:
                pass

        await asyncio.sleep(0.05)

        # Should have restarted once
        assert len(service._tasks) == task_count_after_start + 1

        # Fail again - should exceed max retries
        for i, task in enumerate(list(service._tasks)):
            task.cancel()
            try:
                await task
            except (Exception, asyncio.CancelledError):
                pass
            fail_task = asyncio.create_task(_fail_immediately())
            fail_task.add_done_callback(
                lambda t, n=f"input:{adapter.name}": service._bridge_done_callback(t, n)
            )
            service._tasks[i] = fail_task
            try:
                await fail_task
            except RuntimeError:
                pass

        await asyncio.sleep(0.05)

        # Should not restart again
        assert len(service._tasks) == task_count_after_start + 1
        await service.stop()

    @pytest.mark.asyncio
    async def test_backoff_increases(self):
        broker = FakeBroker()
        adapter = FakeInputAdapter("in1")
        service = BrokerBridgeService(
            broker=broker,
            input_bindings={adapter: Address(kind="test", name="addr1")},
            restart_on_failure=True,
            restart_max_retries=3,
            restart_backoff_seconds=0.01,
        )

        await service.start()
        task = service._tasks[0]
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        fail_task = asyncio.create_task(_fail_immediately())
        fail_task.add_done_callback(
            lambda t, n=f"input:{adapter.name}": service._bridge_done_callback(t, n)
        )
        service._tasks[0] = fail_task
        try:
            await fail_task
        except RuntimeError:
            pass

        await asyncio.sleep(0.02)
        # First restart (backoff = 0.01)
        assert len(service._tasks) == 2
        assert service._restart_counts[f"input:{adapter.name}"] == 1

        task = service._tasks[1]
        task.cancel()
        try:
            await task
        except (Exception, asyncio.CancelledError):
            pass

        fail_task = asyncio.create_task(_fail_immediately())
        fail_task.add_done_callback(
            lambda t, n=f"input:{adapter.name}": service._bridge_done_callback(t, n)
        )
        service._tasks[1] = fail_task
        try:
            await fail_task
        except RuntimeError:
            pass

        await asyncio.sleep(0.04)
        # Second restart (backoff = 0.02)
        assert len(service._tasks) == 3
        assert service._restart_counts[f"input:{adapter.name}"] == 2

        await service.stop()
