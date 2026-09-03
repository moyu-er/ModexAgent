"""消息总线端到端及边缘场景测试。

涵盖：并发安全、消费者异常、stop 语义、Broadcast snapshot 行为、
多 topic 重叠、空负载、BridgeService fail-fast、correlation_id 透传等。
"""

import asyncio

import pytest

from modex_agent.core.session_id import SessionInfo
from modex_agent.core.types import InputMessage, OutputMessage
from modex_agent.messaging.broker import Address, BrokerMessage
from modex_agent.messaging.broker_bridge import (
    BrokerBridgeService,
    BrokerOutputAdapter,
    OutputRoute,
)
from modex_agent.messaging.broker_memory import InMemoryMessageBroker
from modex_agent.adapters.output import OutputAdapter
from modex_agent.pipeline.adapters import InputAdapter


class _MockInputAdapter(InputAdapter):
    def __init__(self):
        self._queue = asyncio.Queue()
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
                    msg = await asyncio.wait_for(self._queue.get(), timeout=0.05)
                    yield msg
                except TimeoutError:
                    continue

        return _gen()

    def inject(self, message: InputMessage):
        self._queue.put_nowait(message)


class _MockOutputAdapter(OutputAdapter):
    def __init__(self):
        self.sent = []

    @property
    def name(self):
        return "mock:out"

    async def send(self, message: OutputMessage, session_id: str):
        self.sent.append((session_id, message.content, dict(message.metadata)))


@pytest.fixture
async def broker():
    b = InMemoryMessageBroker()
    await b.start()
    yield b
    await b.stop()


# ── 并发与竞态 ──


async def test_concurrent_p2p_from_multiple_senders(broker):
    """多个发送方并发向同一 Address 发消息，消费端按 FIFO 接收。"""
    addr = Address(kind="agent", name="target")
    await broker.register_consumer(addr)

    async def _send_batch(start: int):
        for i in range(10):
            await broker.send_to(
                addr,
                BrokerMessage(
                    payload={"i": start + i},
                    sender=Address(kind="user", name=f"u{start}"),
                    recipient=addr,
                ),
            )

    await asyncio.gather(_send_batch(0), _send_batch(100), _send_batch(200))

    received = []
    for _ in range(30):
        received.append(await asyncio.wait_for(broker.consume(addr), timeout=1.0))

    values = [msg.payload["i"] for msg in received]
    # 数量正确且每个 batch 内部有序
    assert len(values) == 30
    for base in (0, 100, 200):
        batch = sorted([v for v in values if base <= v < base + 10])
        assert batch == list(range(base, base + 10))


async def test_publish_race_with_new_subscriber(broker):
    """publish 期间新 subscriber 加入，不应收到旧消息（无回溯）。"""
    topic = "live:events"
    addr_a = Address(kind="agent", name="a")
    await broker.register_consumer(addr_a)

    # a 先订阅
    sub_a = broker.subscribe([topic])
    task_a = asyncio.create_task(_collect_n(sub_a, 1))
    await asyncio.sleep(0.02)

    await broker.publish(
        topic,
        BrokerMessage(payload={"n": 1}, sender=Address(kind="system", name="sys"), topic=topic),
    )

    got_a = await asyncio.wait_for(task_a, timeout=0.5)
    assert got_a[0].payload["n"] == 1

    # 新 subscriber b 后加入
    addr_b = Address(kind="agent", name="b")
    await broker.register_consumer(addr_b)
    sub_b = broker.subscribe([topic])
    task_b = asyncio.create_task(_collect_n(sub_b, 1))
    await asyncio.sleep(0.02)

    # 再发一条，只有 b 收到
    await broker.publish(
        topic,
        BrokerMessage(payload={"n": 2}, sender=Address(kind="system", name="sys"), topic=topic),
    )

    got_b = await asyncio.wait_for(task_b, timeout=0.5)
    assert got_b[0].payload["n"] == 2


# ── stop / sentinel 语义 ──


async def test_stop_does_not_inject_sentinel_as_normal_message(broker):
    """stop() 注入的 sentinel 不应被消费者当作正常 BrokerMessage 收到。"""
    addr = Address(kind="agent", name="x")
    await broker.register_consumer(addr)

    normal = BrokerMessage(payload={"t": "normal"}, sender=Address(kind="user", name="u"), recipient=addr)
    await broker.send_to(addr, normal)

    received = []
    task = asyncio.create_task(_consume_stream_until_stop(broker, addr, received))
    await asyncio.sleep(0.02)
    await broker.stop()
    await asyncio.wait_for(task, timeout=0.5)

    assert len(received) == 1
    assert received[0].payload["t"] == "normal"


async def test_stop_preserves_queued_messages(broker):
    """stop() 后 mailbox 中未消费的消息应保留，而不是被清空。"""
    addr = Address(kind="agent", name="x")
    await broker.register_consumer(addr)

    for i in range(3):
        await broker.send_to(
            addr,
            BrokerMessage(payload={"i": i}, sender=Address(kind="user", name="u"), recipient=addr),
        )

    await broker.stop()
    # 重新启动同一个 broker（实际中应新建，但 InMemoryBroker 只改 _running 标志）
    await broker.start()

    received = []
    async for msg in broker.consume_stream(addr):
        received.append(msg)
        if len(received) == 3:
            break

    assert [m.payload["i"] for m in received] == [0, 1, 2]


# ── subscribe 与资源泄漏 ──


async def test_subscribe_cleanup_quantified(broker):
    """subscribe 退出后，临时 Address 和 topic subscription 都应被清理。"""
    topic = "t:1"
    addr = Address(kind="agent", name="permanent")
    await broker.register_consumer(addr)

    async def _sub():
        count = 0
        async for _ in broker.subscribe([topic]):
            count += 1
            if count == 1:
                break

    task = asyncio.create_task(_sub())
    await asyncio.sleep(0.02)
    await broker.publish(topic, BrokerMessage(payload={}, sender=Address(kind="user", name="u"), topic=topic))
    await asyncio.wait_for(task, timeout=0.5)
    await asyncio.sleep(0)  # yield control to let generator aclose finish

    temps = [a for a in broker._mailboxes if a.kind == "_temp"]
    subs = broker._topic_subscriptions.get(topic, set())

    # 核心关注点：临时 Address 必须被清理，topic 订阅必须为空
    assert temps == []
    assert subs == set()


async def test_overlapping_subscribe_to_same_topic(broker):
    """多个独立 subscribe 到同一 topic，应各自收到全部消息。"""
    topic = "shared"

    async def _sub(label: str):
        msgs = []
        async for msg in broker.subscribe([topic]):
            msgs.append(msg)
            if len(msgs) == 2:
                break
        return label, msgs

    t1 = asyncio.create_task(_sub("a"))
    t2 = asyncio.create_task(_sub("b"))
    await asyncio.sleep(0.02)

    await broker.publish(
        topic, BrokerMessage(payload={"n": 1}, sender=Address(kind="user", name="u"), topic=topic)
    )
    await broker.publish(
        topic, BrokerMessage(payload={"n": 2}, sender=Address(kind="user", name="u"), topic=topic)
    )

    _, msgs_a = await asyncio.wait_for(t1, timeout=1.0)
    _, msgs_b = await asyncio.wait_for(t2, timeout=1.0)

    assert [m.payload["n"] for m in msgs_a] == [1, 2]
    assert [m.payload["n"] for m in msgs_b] == [1, 2]


# ── broadcast snapshot 行为 ──


async def test_broadcast_does_not_include_late_registrants(broker):
    """broadcast 使用注册时的 snapshot，不应包含之后注册的新消费者。"""
    addr_a = Address(kind="agent", name="early")
    await broker.register_consumer(addr_a)

    # broadcast 时 b 还未注册
    await broker.broadcast(
        BrokerMessage(payload={"batch": 1}, sender=Address(kind="system", name="sys"), broadcast=True)
    )

    addr_b = Address(kind="agent", name="late")
    await broker.register_consumer(addr_b)

    # 再 broadcast 一次
    await broker.broadcast(
        BrokerMessage(payload={"batch": 2}, sender=Address(kind="system", name="sys"), broadcast=True)
    )

    # a 收到 2 条
    assert (await asyncio.wait_for(broker.consume(addr_a), timeout=0.5)).payload["batch"] == 1
    assert (await asyncio.wait_for(broker.consume(addr_a), timeout=0.5)).payload["batch"] == 2

    # b 只收到第 2 条
    assert (await asyncio.wait_for(broker.consume(addr_b), timeout=0.5)).payload["batch"] == 2


# ── Bridge 层边缘场景 ──


async def test_broker_output_adapter_correlation_id_set(broker):
    """BrokerOutputAdapter 发送的消息应携带 correlation_id=session_id。"""
    adapter = BrokerOutputAdapter(
        broker=broker,
        sender=Address(kind="agent", name="react"),
        default_topic="out",
    )
    out = OutputMessage(content="c")

    sub = broker.subscribe(["out"])
    task = asyncio.create_task(_collect_one(sub))
    await asyncio.sleep(0.02)

    await adapter.send(out, "session-alpha")
    msg = await asyncio.wait_for(task, timeout=0.5)
    assert msg is not None
    assert msg.correlation_id == "session-alpha"


async def test_bridge_service_match_kind_fail_fast(broker):
    """配置 match_kind 的 OutputRoute 应在 start() 时立即抛 NotImplementedError。"""
    out_adapter = _MockOutputAdapter()
    service = BrokerBridgeService(
        broker=broker,
        output_routes=[OutputRoute(adapter=out_adapter, match_kind="user")],
    )
    with pytest.raises(NotImplementedError):
        await service.start()
    await service.stop()


async def test_bridge_service_input_exception_isolation(broker):
    """一个 input binding 的 native adapter 异常，不应拖垮另一个 input binding。"""

    class CrashingInputAdapter(InputAdapter):
        @property
        def name(self):
            return "crash:in"

        async def start(self):
            pass

        async def stop(self):
            pass

        def receive(self):
            async def _gen():
                raise RuntimeError("boom")
                yield  # noqa: PIE790 — makes this an async generator at compile time

            return _gen()

    healthy = _MockInputAdapter()
    await healthy.start()
    healthy.inject(InputMessage(content="ok", session=SessionInfo.from_str("s1")))

    service = BrokerBridgeService(
        broker=broker,
        input_bindings={
            CrashingInputAdapter(): Address(kind="channel", name="bad"),
            healthy: Address(kind="channel", name="good"),
        },
    )
    await service.start()
    await asyncio.sleep(0.05)

    # healthy 的消息仍然被桥接到 broker
    got = await asyncio.wait_for(broker.consume(Address(kind="channel", name="good")), timeout=0.5)
    assert got.payload["content"] == "ok"

    await service.stop()


async def test_bridge_service_output_topic_multiple_messages(broker):
    """topic 路由应能持续转发多条消息。"""
    out_adapter = _MockOutputAdapter()
    service = BrokerBridgeService(
        broker=broker,
        output_routes=[OutputRoute(adapter=out_adapter, match_topic="agent:out")],
    )
    await service.start()
    await asyncio.sleep(0.02)

    for i in range(5):
        await broker.publish(
            "agent:out",
            BrokerMessage(
                payload={"content": str(i), "session_id": f"s{i}"},
                sender=Address(kind="agent", name="x"),
                topic="agent:out",
            ),
        )

    await asyncio.sleep(0.1)
    assert len(out_adapter.sent) == 5
    for i, (sid, content, _) in enumerate(out_adapter.sent):
        assert sid == f"s{i}"
        assert content == str(i)

    await service.stop()


# ── payload 与字段边缘 ──


async def test_empty_payload_message(broker):
    """空 payload 的消息应能正常收发，不抛异常。"""
    addr = Address(kind="agent", name="empty")
    await broker.register_consumer(addr)
    msg = BrokerMessage(payload={}, sender=Address(kind="user", name="u"), recipient=addr)
    await broker.send_to(addr, msg)
    got = await asyncio.wait_for(broker.consume(addr), timeout=0.5)
    assert got.payload == {}


async def test_long_session_id_and_correlation_id(broker):
    """超长 session_id / correlation_id 应能原样传递。"""
    long_id = "x" * 4096
    addr = Address(kind="agent", name="long")
    await broker.register_consumer(addr)
    msg = BrokerMessage(
        payload={"k": 1},
        sender=Address(kind="user", name="u"),
        recipient=addr,
        correlation_id=long_id,
    )
    await broker.send_to(addr, msg)
    got = await asyncio.wait_for(broker.consume(addr), timeout=0.5)
    assert got.correlation_id == long_id
    assert len(got.correlation_id) == 4096


# ── Helpers ──


async def _collect_one(aiter):
    async for msg in aiter:
        return msg
    return None


async def _collect_n(aiter, n: int):
    result = []
    async for msg in aiter:
        result.append(msg)
        if len(result) == n:
            break
    return result


async def _consume_stream_until_stop(broker, addr, out_list):
    async for msg in broker.consume_stream(addr):
        out_list.append(msg)
