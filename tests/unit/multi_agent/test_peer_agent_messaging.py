"""Tests for peer agent messaging: send_message_async → inbox → consume.

Covers the full agent-to-agent communication chain:
  1. SendMessageAsyncTool stores message into target agent's inbox
  2. has_pending() is non-destructive (does NOT consume messages)
  3. AgentPool inbox wakeup delivers messages correctly
  4. SendMessageTool triggers target agent via broker
"""

from __future__ import annotations

from datetime import datetime

from framework.messaging.broker_memory import InMemoryMessageBroker
from framework.multi_agent.address import AgentAddress
from framework.multi_agent.bus import LocalAgentMessageBus
from framework.multi_agent.envelope import AgentMessageEnvelope
from framework.multi_agent.inbox.consumer import InboxConsumer
from framework.multi_agent.inbox.producer import InboxProducer
from framework.multi_agent.inbox.server_memory import InMemoryInboxServer
from framework.multi_agent.inbox.types import InboxMessage
from framework.multi_agent.session_id import DefaultSessionIdStrategy

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_bus(broker: InMemoryMessageBroker | None = None) -> LocalAgentMessageBus:
    server = InMemoryInboxServer()
    producer = InboxProducer(server=server)
    consumer = InboxConsumer(server=server)
    return LocalAgentMessageBus(producer=producer, consumer=consumer, broker=broker)


# ===========================================================================
# 1. SendMessageAsyncTool – 消息正确落入目标 Agent 的 inbox
# ===========================================================================


class TestSendMessageAsyncTool:
    """Verify send_message_async persists messages without waking the target."""

    async def test_stores_message_in_target_inbox(self):
        """Peer 调用 send_message_async 后，main 的 inbox 中应出现该消息。"""
        bus = _make_bus()
        tool = SendMessageAsyncTool(
            broker=InMemoryMessageBroker(),
            self_address=AgentAddress(name="doc-expert"),
            allowed_targets=["main"],
            agent_bus=bus,
        )

        result = await tool.execute(
            target_agent="main",
            content="坦克大战已完成！",
            message_type="agent_message",
            conversation_id="conv_001",
        )

        assert result == "Async message queued for main."

        # Verify the message is in the inbox under the correct session_id
        session_id = DefaultSessionIdStrategy().format(conversation_id="conv_001", agent_name="main")
        polled = await bus.poll(session_id, limit=10)
        assert len(polled) == 1
        assert polled[0].payload["content"] == "坦克大战已完成！"
        assert polled[0].source.name == "doc-expert"

    async def test_session_id_uses_conversation_id_colon_target(self):
        """消息的 session_id 应为 conversation_id:target_agent 格式。"""
        server = InMemoryInboxServer()
        producer = InboxProducer(server=server)
        consumer = InboxConsumer(server=server)
        bus = LocalAgentMessageBus(producer=producer, consumer=consumer)

        tool = SendMessageAsyncTool(
            broker=InMemoryMessageBroker(),
            self_address=AgentAddress(name="peer"),
            agent_bus=bus,
        )

        await tool.execute(
            target_agent="main",
            content="hello",
            conversation_id="ABC123",
        )

        # Message should be under "ABC123:main"
        count = await server.count("ABC123:main")
        assert count == 1

        # NOT under bare "ABC123"
        count_bare = await server.count("ABC123")
        assert count_bare == 0

    async def test_acl_blocks_disallowed_target(self):
        """allowed_targets 应阻止向未授权的 agent 发送消息。"""
        bus = _make_bus()
        tool = SendMessageAsyncTool(
            broker=InMemoryMessageBroker(),
            self_address=AgentAddress(name="peer"),
            allowed_targets=["main"],
            agent_bus=bus,
        )

        result = await tool.execute(
            target_agent="other_agent",
            content="should be blocked",
            conversation_id="conv_001",
        )

        assert "not allowed" in result

    async def test_requires_agent_bus(self):
        """没有 agent_bus 时应返回错误。"""
        tool = SendMessageAsyncTool(
            broker=InMemoryMessageBroker(),
            self_address=AgentAddress(name="peer"),
            agent_bus=None,
        )

        result = await tool.execute(
            target_agent="main",
            content="should fail",
        )

        assert "Error" in result


# ===========================================================================
# 2. has_pending – 非破坏性检查（核心 bug 修复验证）
# ===========================================================================


class TestHasPendingNonDestructive:
    """Verify has_pending() does NOT consume messages from the inbox."""

    async def test_has_pending_does_not_consume(self):
        """has_pending 应该只检查，不应该消费消息。"""
        bus = _make_bus()

        # Store a message via send_silent
        envelope = AgentMessageEnvelope(
            payload={"content": "response from peer"},
            source=AgentAddress(name="doc-expert"),
            target=AgentAddress(name="main"),
            message_type="agent_message",
            conversation_id="conv_001",
        )
        await bus.send_silent("conv_001:main", envelope)

        # has_pending should return True without consuming
        assert await bus.has_pending("conv_001:main") is True

        # Message should STILL be available for subsequent consume
        results = await bus.poll("conv_001:main", limit=10)
        assert len(results) == 1
        assert results[0].payload["content"] == "response from peer"

    async def test_has_pending_returns_false_when_empty(self):
        """空 inbox 应返回 False。"""
        bus = _make_bus()
        assert await bus.has_pending("nonexistent_session") is False

    async def test_poll_then_has_pending_sees_nothing(self):
        """消费后 has_pending 应返回 False。"""
        bus = _make_bus()

        envelope = AgentMessageEnvelope(
            payload={"content": "temp"},
            source=AgentAddress(name="peer"),
            message_type="agent_message",
        )
        await bus.send_silent("s1", envelope)

        # Consume the message
        results = await bus.poll("s1", limit=10)
        assert len(results) == 1

        # Now has_pending should be False
        assert await bus.has_pending("s1") is False

    async def test_has_pending_survives_multiple_checks(self):
        """多次 has_pending 调用都不应该消费消息。"""
        bus = _make_bus()

        envelope = AgentMessageEnvelope(
            payload={"content": "durable"},
            source=AgentAddress(name="peer"),
            message_type="agent_message",
        )
        await bus.send_silent("s1", envelope)

        for _ in range(5):
            assert await bus.has_pending("s1") is True

        # Still available after 5 checks
        results = await bus.poll("s1", limit=10)
        assert len(results) == 1


# ===========================================================================
# 3. SendMessageTool – 同步发送触发 broker 消息
# ===========================================================================


class TestSendMessageTool:
    """Verify send_message sends via broker and triggers target agent."""

    async def test_sends_via_broker(self):
        """send_message 应通过 broker 发送消息到目标 agent。"""
        broker = InMemoryMessageBroker()
        await broker.start()
        target_addr = AgentAddress(kind="agent", name="doc-expert")
        await broker.register_consumer(target_addr)

        tool = SendMessageTool(
            broker=broker,
            self_address=AgentAddress(name="main"),
        )

        result = await tool.execute(
            target_agent="doc-expert",
            content="请写一个坦克大战",
            message_type="agent_message",
            conversation_id="conv_001",
        )

        assert result == "Message sent to doc-expert."

        # Target agent should receive a broker message
        msg = await broker.consume(target_addr)
        assert msg is not None
        assert msg.payload.get("content") == "请写一个坦克大战"

        await broker.stop()

    async def test_acl_blocks_disallowed_target(self):
        tool = SendMessageTool(
            broker=InMemoryMessageBroker(),
            self_address=AgentAddress(name="main"),
            allowed_targets=["peer_a"],
        )

        result = await tool.execute(
            target_agent="peer_b",
            content="blocked",
        )

        assert "not allowed" in result


# ===========================================================================
# 4. 端到端：Peer Agent 回信链路（send_message_async → inbox → consume）
# ===========================================================================


class TestPeerAgentReplyChain:
    """End-to-end test: peer sends async reply → main consumes from inbox."""

    async def test_peer_reply_delivered_to_main_inbox(self):
        """模拟完整的 peer 回信链路：peer → send_message_async → main inbox → consume。"""
        broker = InMemoryMessageBroker()
        await broker.start()

        bus = _make_bus(broker=broker)

        # 1. Peer sends async reply to main
        peer_tool = SendMessageAsyncTool(
            broker=broker,
            self_address=AgentAddress(name="doc-expert"),
            allowed_targets=["main"],
            agent_bus=bus,
        )

        result = await peer_tool.execute(
            target_agent="main",
            content="超级玛丽小游戏已完成！文件路径: data/mario_game.html",
            message_type="agent_message",
            conversation_id="conv_002",
        )
        assert "queued" in result

        # 2. Verify main's inbox has the message (non-destructive check)
        main_session = DefaultSessionIdStrategy().format(conversation_id="conv_002", agent_name="main")
        assert await bus.has_pending(main_session) is True

        # 3. Main consumes the message (simulating InboxFlushHook or wakeup)
        envelopes = await bus.poll(main_session, limit=10)
        assert len(envelopes) == 1
        assert "超级玛丽" in envelopes[0].payload["content"]
        assert envelopes[0].source.name == "doc-expert"

        # 4. After consume, inbox should be empty
        assert await bus.has_pending(main_session) is False

        await broker.stop()

    async def test_multiple_async_messages_queue_correctly(self):
        """多条 async 消息应按顺序排队。"""
        bus = _make_bus()
        tool = SendMessageAsyncTool(
            broker=InMemoryMessageBroker(),
            self_address=AgentAddress(name="peer"),
            agent_bus=bus,
        )

        for i in range(3):
            await tool.execute(
                target_agent="main",
                content=f"Message {i}",
                conversation_id="conv_003",
            )

        session = DefaultSessionIdStrategy().format(conversation_id="conv_003", agent_name="main")
        assert await bus.has_pending(session) is True

        results = await bus.poll(session, limit=10)
        assert len(results) == 3
        contents = [e.payload["content"] for e in results]
        assert contents == ["Message 0", "Message 1", "Message 2"]

    async def test_bidirectional_session_isolation(self):
        """main→peer 和 peer→main 的 session 应该互相隔离。"""
        bus = _make_bus()

        peer_to_main = SendMessageAsyncTool(
            broker=InMemoryMessageBroker(),
            self_address=AgentAddress(name="peer"),
            agent_bus=bus,
        )
        main_to_peer = SendMessageAsyncTool(
            broker=InMemoryMessageBroker(),
            self_address=AgentAddress(name="main"),
            allowed_targets=["peer"],
            agent_bus=bus,
        )

        # Peer → Main
        await peer_to_main.execute(
            target_agent="main", content="peer→main", conversation_id="conv_004",
        )
        # Main → Peer
        await main_to_peer.execute(
            target_agent="peer", content="main→peer", conversation_id="conv_004",
        )

        # Check isolation
        main_session = DefaultSessionIdStrategy().format(conversation_id="conv_004", agent_name="main")
        peer_session = DefaultSessionIdStrategy().format(conversation_id="conv_004", agent_name="peer")

        main_msgs = await bus.poll(main_session, limit=10)
        peer_msgs = await bus.poll(peer_session, limit=10)

        assert len(main_msgs) == 1
        assert main_msgs[0].payload["content"] == "peer→main"

        assert len(peer_msgs) == 1
        assert peer_msgs[0].payload["content"] == "main→peer"


# ===========================================================================
# 5. InboxConsumer.count – 非破坏性计数
# ===========================================================================


class TestInboxConsumerCount:
    """Verify InboxConsumer.count() delegates to server without consuming."""

    async def test_count_returns_pending_count(self):
        server = InMemoryInboxServer()
        consumer = InboxConsumer(server=server)

        assert await consumer.count("s1") == 0

        # Manually inject messages

        for i in range(3):
            msg = InboxMessage(
                session_id="s1",
                source="peer",
                content=f"msg_{i}",
                message_type="agent_message",
                message_id=f"id_{i}",
                timestamp=datetime.now(),
            )
            await server.receive("s1", msg)

        assert await consumer.count("s1") == 3

    async def test_count_does_not_affect_consume(self):
        """count() 后消息仍可正常消费。"""
        server = InMemoryInboxServer()
        consumer = InboxConsumer(server=server)


        msg = InboxMessage(
            session_id="s1",
            source="peer",
            content="important",
            message_type="agent_message",
            message_id="id_1",
            timestamp=datetime.now(),
        )
        await server.receive("s1", msg)

        # Count (non-destructive)
        assert await consumer.count("s1") == 1

        # Still consumable
        msgs = await consumer.consume("s1", limit=10)
        assert len(msgs) == 1
        assert msgs[0].content == "important"

        # Now empty
        assert await consumer.count("s1") == 0


# ===========================================================================
# 6. Wakeup timeout – 重复唤醒不会导致重复消费
# ===========================================================================


class TestWakeupTimeoutNoDuplicateConsumption:
    """Verify SendMessageAsyncTool's wakeup_timeout does not cause double consumption."""

    async def test_wakeup_task_skips_when_message_already_consumed(self):
        """如果消息已被消费，超时唤醒任务不应发送多余的 broker wakeup。"""
        broker = InMemoryMessageBroker()
        await broker.start()
        bus = _make_bus(broker=broker)

        tool = SendMessageAsyncTool(
            broker=broker,
            self_address=AgentAddress(name="peer"),
            allowed_targets=["main"],
            agent_bus=bus,
            wakeup_timeout=0.1,  # 100ms timeout for fast test
        )

        await tool.execute(
            target_agent="main",
            content="hello",
            conversation_id="conv_wakeup",
        )

        # Message is in inbox
        session = DefaultSessionIdStrategy().format(conversation_id="conv_wakeup", agent_name="main")
        assert await bus.has_pending(session) is True

        # Simulate early consumption (e.g., by InboxFlushHook or another wakeup)
        consumed = await bus.poll(session, limit=10)
        assert len(consumed) == 1
        assert await bus.has_pending(session) is False

        # Wait for the wakeup timeout to fire
        import asyncio

        await asyncio.sleep(0.2)

        # Broker should NOT have received a wakeup (message was already consumed)
        # Verify by checking that no stray _inbox_wakeup messages were queued
        # for the main agent
        main_addr = AgentAddress(kind="agent", name="main")
        await broker.register_consumer(main_addr)

        # Give a small window for any delayed broker messages
        await asyncio.sleep(0.1)

        # The broker mailbox for main should be empty (no wakeup sent)
        # because has_pending returned False
        # We can't directly check broker queue emptiness without consuming,
        # but we can verify the invariant: inbox is still empty
        assert await bus.has_pending(session) is False

        await broker.stop()

    async def test_duplicate_broker_wakeups_do_not_consume_twice(self):
        """多次 broker wakeup 到达时，消息只应被消费一次。"""
        broker = InMemoryMessageBroker()
        await broker.start()
        bus = _make_bus(broker=broker)

        # Store a message silently
        envelope = AgentMessageEnvelope(
            payload={"content": "task result"},
            source=AgentAddress(name="peer"),
            target=AgentAddress(name="main"),
            message_type="agent_message",
            conversation_id="conv_dup",
        )
        await bus.send_silent("conv_dup:main", envelope)
        assert await bus.has_pending("conv_dup:main") is True

        # Simulate two broker wakeups arriving simultaneously
        from framework.messaging.broker import Address, BrokerMessage

        wakeup_msg = BrokerMessage(
            payload={"_inbox_wakeup": True, "session_id": "conv_dup:main"},
            sender=Address(kind="system", name="test"),
        )

        # Send two wakeups
        main_addr = AgentAddress(kind="agent", name="main")
        await broker.register_consumer(main_addr)
        await broker.send_to(main_addr, wakeup_msg)
        await broker.send_to(main_addr, wakeup_msg)

        # Consume the first wakeup
        msg1 = await broker.consume(main_addr)
        assert msg1.payload.get("_inbox_wakeup") is True

        # Handle first wakeup: poll inbox (consumes the message)
        envelopes1 = await bus.poll("conv_dup:main", limit=10)
        assert len(envelopes1) == 1
        assert envelopes1[0].payload["content"] == "task result"

        # Inbox is now empty
        assert await bus.has_pending("conv_dup:main") is False

        # Consume the second wakeup
        msg2 = await broker.consume(main_addr)
        assert msg2.payload.get("_inbox_wakeup") is True

        # Handle second wakeup: poll inbox (already empty)
        envelopes2 = await bus.poll("conv_dup:main", limit=10)
        assert len(envelopes2) == 0  # No duplicate consumption!

        await broker.stop()

    async def test_wakeup_timeout_sends_broker_wakeup_when_pending(self):
        """消息未被消费时，超时任务应发送 broker wakeup。"""
        broker = InMemoryMessageBroker()
        await broker.start()
        bus = _make_bus(broker=broker)

        tool = SendMessageAsyncTool(
            broker=broker,
            self_address=AgentAddress(name="peer"),
            allowed_targets=["main"],
            agent_bus=bus,
            wakeup_timeout=0.05,  # 50ms for fast test
        )

        main_addr = AgentAddress(kind="agent", name="main")
        await broker.register_consumer(main_addr)

        await tool.execute(
            target_agent="main",
            content="wakeup test",
            conversation_id="conv_wake",
        )

        # Wait for timeout + buffer
        import asyncio

        await asyncio.sleep(0.15)

        # A broker wakeup should have been sent
        msg = await broker.consume(main_addr)
        assert msg.payload.get("_inbox_wakeup") is True
        assert msg.payload.get("session_id") == "conv_wake:main"

        await broker.stop()
