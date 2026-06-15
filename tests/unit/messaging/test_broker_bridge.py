import asyncio
import pytest

from framework.core.session_id import SessionInfo
from framework.core.types import InputMessage, OutputMessage
from framework.messaging.broker import Address, BrokerMessage
from framework.messaging.broker_bridge import (
    BrokerBridgeService,
    BrokerInputAdapter,
    BrokerOutputAdapter,
    OutputRoute,
    _broker_msg_to_input_message,
)
from framework.messaging.broker_memory import InMemoryMessageBroker
from framework.pipeline.adapters import InputAdapter, OutputAdapter


class MockInputAdapter(InputAdapter):
    def __init__(self, messages=None):
        self.messages = messages or []
        self._queue = asyncio.Queue()
        for m in self.messages:
            self._queue.put_nowait(m)
        self._running = False

    @property
    def name(self):
        return "mock:in"

    async def start(self):
        self._running = True

    async def stop(self):
        self._running = False

    def receive(self):
        async def _gen():
            while self._running:
                try:
                    msg = await asyncio.wait_for(self._queue.get(), timeout=0.1)
                    yield msg
                except asyncio.TimeoutError:
                    continue
        return _gen()

    def inject(self, message: InputMessage):
        self._queue.put_nowait(message)


class MockOutputAdapter(OutputAdapter):
    def __init__(self):
        self.sent = []

    @property
    def name(self):
        return "mock:out"

    async def send(self, message: OutputMessage, session_id: str):
        self.sent.append((session_id, message.content, message.metadata))


@pytest.fixture
async def broker():
    b = InMemoryMessageBroker()
    await b.start()
    yield b
    await b.stop()


async def test_broker_input_adapter_receives_messages(broker):
    addr = Address("channel", "qq")
    adapter = BrokerInputAdapter(broker, addr)
    await adapter.start()

    msg = BrokerMessage(
        payload={"content": "hello", "session_id": "s1"},
        sender=Address("user", "123"),
        recipient=addr,
    )
    await broker.send_to(addr, msg)

    received = []
    async for im in adapter.receive():
        received.append(im)
        if len(received) == 1:
            break

    await adapter.stop()
    assert len(received) == 1
    assert received[0].content == "hello"
    assert str(received[0].session) == "s1"
    assert received[0].source == "user:123"


async def test_broker_input_adapter_preserves_metadata(broker):
    addr = Address("channel", "qq")
    adapter = BrokerInputAdapter(broker, addr)

    msg = BrokerMessage(
        payload={"content": "hi"},
        sender=Address("user", "123456"),
        recipient=addr,
        headers={"channel": "qq", "chat_id": "789"},
    )
    im = _broker_msg_to_input_message(msg)
    assert im.sender_id == "123456"
    assert im.channel == "qq"
    assert im.chat_id == "789"


async def test_broker_output_adapter_sends_via_topic(broker):
    adapter = BrokerOutputAdapter(
        broker=broker,
        sender=Address("agent", "react"),
        default_topic="agent:outgoing",
    )
    out = OutputMessage(content="reply", metadata={"k": "v"})

    sub = broker.subscribe(["agent:outgoing"])
    task = asyncio.create_task(_collect_one(sub))
    await asyncio.sleep(0.05)

    await adapter.send(out, "session_42")

    got = await asyncio.wait_for(task, timeout=0.5)
    assert got is not None
    assert got.payload["content"] == "reply"
    assert got.payload["session_id"] == "session_42"
    assert got.sender == Address("agent", "react")


async def test_broker_output_adapter_sends_via_recipient(broker):
    recipient = Address("user", "999")
    adapter = BrokerOutputAdapter(
        broker=broker,
        sender=Address("agent", "sales"),
        default_recipient=recipient,
    )
    out = OutputMessage(content="quote")
    await adapter.send(out, "s1")

    await broker.register_consumer(recipient)
    got = await asyncio.wait_for(broker.consume(recipient), timeout=0.5)
    assert got.payload["content"] == "quote"
    assert got.sender == Address("agent", "sales")


async def test_broker_output_adapter_requires_sender():
    b = InMemoryMessageBroker()
    with pytest.raises(ValueError, match="Must provide default_recipient or default_topic"):
        BrokerOutputAdapter(broker=b, sender=Address("agent", "x"))


async def test_bridge_service_input_binding(broker):
    mock_in = MockInputAdapter()
    await mock_in.start()
    bound_addr = Address("channel", "qq")

    service = BrokerBridgeService(
        broker=broker,
        input_bindings={mock_in: bound_addr},
    )
    await service.start()

    mock_in.inject(InputMessage(content="from_qq", session=SessionInfo.from_str("s1", default_agent_name="main"), source="qq", sender_id="u1"))

    got = await asyncio.wait_for(broker.consume(bound_addr), timeout=0.5)
    assert got.payload["content"] == "from_qq"
    assert got.sender == Address("channel", "qq")

    await service.stop()


async def test_bridge_service_output_topic_routing(broker):
    mock_out = MockOutputAdapter()
    service = BrokerBridgeService(
        broker=broker,
        output_routes=[OutputRoute(adapter=mock_out, match_topic="agent:outgoing")],
    )
    await service.start()
    await asyncio.sleep(0.05)  # let subscriber task reach its first await

    await broker.publish(
        "agent:outgoing",
        BrokerMessage(
            payload={"content": " routed", "session_id": "s2", "metadata": {}},
            sender=Address("agent", "react"),
            topic="agent:outgoing",
        ),
    )

    await asyncio.sleep(0.1)
    assert len(mock_out.sent) == 1
    assert mock_out.sent[0] == ("s2", " routed", {})

    await service.stop()


async def test_bridge_service_stop_gracefully(broker):
    mock_in = MockInputAdapter()
    await mock_in.start()
    mock_out = MockOutputAdapter()

    service = BrokerBridgeService(
        broker=broker,
        input_bindings={mock_in: Address("channel", "qq")},
        output_routes=[OutputRoute(adapter=mock_out, match_topic="t1")],
    )
    await service.start()

    # Ensure tasks are running
    assert len(service._tasks) == 2

    await service.stop()
    # After stop, broker should be stopped (running=False)
    assert not broker._running


async def _collect_one(aiter):
    async for msg in aiter:
        return msg
    return None
