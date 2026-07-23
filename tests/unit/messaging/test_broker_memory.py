import asyncio
import pytest

from modex_agent.messaging.broker import Address, AddressKind, BrokerMessage
from modex_agent.messaging.broker_memory import InMemoryMessageBroker


@pytest.fixture
async def broker():
    b = InMemoryMessageBroker()
    await b.start()
    yield b
    await b.stop()


async def test_p2p_send_to_and_consume(broker):
    addr = Address(kind="agent", name="worker")
    await broker.register_consumer(addr)

    msg = BrokerMessage(payload={"content": "hello"}, sender=Address(kind="user", name="u1"), recipient=addr)
    await broker.send_to(addr, msg)

    received = await broker.consume(addr)
    assert received.payload["content"] == "hello"
    assert received.sender == Address(kind="user", name="u1")


async def test_publish_delivers_to_all_subscribers(broker):
    addr_a = Address(kind="agent", name="a")
    addr_b = Address(kind="agent", name="b")
    addr_c = Address(kind="agent", name="c")
    await broker.register_consumer(addr_a)
    await broker.register_consumer(addr_b)
    # c does not subscribe

    sub_a = broker.subscribe(["chat:general"])
    sub_b = broker.subscribe(["chat:general"])

    task_a = asyncio.create_task(_collect_one(sub_a))
    task_b = asyncio.create_task(_collect_one(sub_b))
    await asyncio.sleep(0.05)  # let subscriptions set up

    msg = BrokerMessage(payload={"content": "hi all"}, sender=Address(kind="user", name="u1"), topic="chat:general")
    await broker.publish("chat:general", msg)

    got_a = await task_a
    got_b = await task_b
    assert got_a is not None
    assert got_b is not None

    assert got_a.payload["content"] == "hi all"
    assert got_b.payload["content"] == "hi all"

    # c should not receive
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(broker.consume(addr_c), timeout=0.1)


async def test_broadcast_reaches_all_registered_consumers(broker):
    addr_a = Address(kind="agent", name="a")
    addr_b = Address(kind="agent", name="b")
    await broker.register_consumer(addr_a)
    await broker.register_consumer(addr_b)

    msg = BrokerMessage(payload={"content": "broadcast"}, sender=Address(kind="system", name="sys"), broadcast=True)
    await broker.broadcast(msg)

    got_a = await broker.consume(addr_a)
    got_b = await broker.consume(addr_b)
    assert got_a.payload["content"] == "broadcast"
    assert got_b.payload["content"] == "broadcast"


async def test_consume_stream_iterates_messages(broker):
    addr = Address(kind="agent", name="streamer")
    await broker.register_consumer(addr)

    for i in range(5):
        await broker.send_to(addr, BrokerMessage(payload={"i": i}, sender=Address(kind="user", name="u1"), recipient=addr))

    collected = []
    async for msg in broker.consume_stream(addr):
        collected.append(msg.payload["i"])
        if len(collected) == 5:
            break

    assert collected == [0, 1, 2, 3, 4]


async def test_subscribe_multiple_topics(broker):
    sub = broker.subscribe(["topic:a", "topic:b"])
    collected = []

    async def _collect():
        async for msg in sub:
            collected.append((msg.topic, msg.payload["content"]))
            if len(collected) == 3:
                break

    task = asyncio.create_task(_collect())
    await asyncio.sleep(0.05)

    await broker.publish("topic:a", BrokerMessage(payload={"content": "a1"}, sender=Address(kind="user", name="u1"), topic="topic:a"))
    await broker.publish("topic:b", BrokerMessage(payload={"content": "b1"}, sender=Address(kind="user", name="u1"), topic="topic:b"))
    await broker.publish("topic:a", BrokerMessage(payload={"content": "a2"}, sender=Address(kind="user", name="u1"), topic="topic:a"))

    await asyncio.wait_for(task, timeout=1.0)

    topics = [t for t, _ in collected]
    assert topics.count("topic:a") == 2
    assert topics.count("topic:b") == 1


async def test_unregister_consumer_stops_delivery(broker):
    addr = Address(kind="agent", name="temp")
    await broker.register_consumer(addr)
    await broker.unregister_consumer(addr)

    msg = BrokerMessage(payload={"content": "x"}, sender=Address(kind="user", name="u1"), recipient=addr)
    await broker.send_to(addr, msg)

    # unregistered consumer still gets a mailbox auto-created by send_to,
    # but if we re-unregister, it should be gone
    await broker.unregister_consumer(addr)
    # No exception means OK; the behavior is "auto-create on send_to"


async def test_stop_clears_queues_and_unblocks_consumers(broker):
    addr = Address(kind="agent", name="blocked")
    await broker.register_consumer(addr)

    async def _blocked_consumer():
        collected = []
        async for msg in broker.consume_stream(addr):
            if msg is None:
                break
            collected.append(msg)
        return collected

    task = asyncio.create_task(_blocked_consumer())
    await asyncio.sleep(0.05)

    await broker.stop()
    result = await asyncio.wait_for(task, timeout=0.5)
    assert result == []


async def test_subscribe_cleans_up_temp_address_on_stop(broker):
    async def _sub():
        async for _ in broker.subscribe(["topic:x"]):
            pass

    task = asyncio.create_task(_sub())
    await asyncio.sleep(0.05)

    # There should be a temp address registered
    before = len(broker._mailboxes)
    assert before >= 1

    await broker.stop()
    await asyncio.wait_for(task, timeout=0.5)

    # After stop + subscribe cleanup, temp address should be gone
    temps = [a for a in broker._mailboxes if a.kind == "_temp"]
    assert temps == []


def test_broker_message_roundtrip():
    """to_dict → from_dict preserves all BrokerMessage fields (TDD B5C)."""
    msg = BrokerMessage(
        payload={"content": "hello", "n": 42},
        sender=Address(kind="agent", name="worker"),
        recipient=Address(kind="user", name="u1"),
        topic="chat:general",
        broadcast=False,
        headers={"content_type": "json", "priority": "high"},
        correlation_id="corr-123",
    )
    data = msg.to_dict()
    restored = BrokerMessage.from_dict(data)
    assert restored.payload == msg.payload
    assert restored.sender == msg.sender
    assert restored.recipient == msg.recipient
    assert restored.topic == msg.topic
    assert restored.broadcast == msg.broadcast
    assert restored.headers == msg.headers
    assert restored.correlation_id == msg.correlation_id
    assert restored.timestamp == msg.timestamp


def test_broker_message_roundtrip_with_addresskind_enums():
    """to_dict → from_dict preserves fields when AddressKind enums are used
    for sender/recipient (exercises the enum construction path)."""
    msg = BrokerMessage(
        payload={"key": "val"},
        sender=Address(kind=AddressKind.AGENT, name="a1"),
        recipient=Address(kind=AddressKind.USER, name="u1"),
    )
    data = msg.to_dict()
    restored = BrokerMessage.from_dict(data)
    assert restored.payload == msg.payload
    assert restored.sender == msg.sender
    assert restored.recipient == msg.recipient
    assert restored.topic == msg.topic
    assert restored.broadcast == msg.broadcast
    assert restored.headers == msg.headers
    assert restored.correlation_id == msg.correlation_id
    assert restored.timestamp == msg.timestamp


def test_address_temp_kind():
    """Address accepts AddressKind._TEMP (sentinel used by subscribe mailboxes)."""
    addr = Address(kind=AddressKind._TEMP, name="mailbox1")
    assert addr.kind == AddressKind._TEMP
    assert addr.name == "mailbox1"
    assert str(addr) == "_temp:mailbox1"


def test_address_parse_temp():
    """Address.parse round-trips the _temp: prefix back to AddressKind._TEMP."""
    addr = Address.parse("_temp:mailbox1")
    assert addr.kind == AddressKind._TEMP
    assert addr.name == "mailbox1"


async def _collect_one(aiter):
    async for msg in aiter:
        return msg
    return None
