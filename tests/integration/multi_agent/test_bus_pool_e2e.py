"""Integration tests for AgentMessageBus + AgentPool + SubagentService(queued)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.integration

from framework.core.types import InputMessage
from framework.messaging.broker_memory import InMemoryMessageBroker
from framework.multi_agent import (
    AgentAddress,
    AgentDescriptor,
    DefaultAgentFactory,
    SessionRetentionPolicy,
)
from framework.multi_agent.bus import LocalAgentMessageBus
from framework.multi_agent.envelope import AgentMessageEnvelope
from framework.multi_agent.inbox.consumer import InboxConsumer
from framework.multi_agent.inbox.producer import InboxProducer
from framework.multi_agent.inbox.server_memory import InMemoryInboxServer
from framework.multi_agent.pool import AgentPool


@pytest.mark.asyncio
async def test_agent_pool_dispatches_inbox_wakeup_to_pipeline():
    """AgentPool 通过 _inbox_wakeup 唤醒后正确分发 inbox 中的 agent_message。"""
    broker = InMemoryMessageBroker()
    await broker.start()

    server = InMemoryInboxServer()
    producer = InboxProducer(server=server)
    consumer = InboxConsumer(server=server)
    bus = LocalAgentMessageBus(producer=producer, consumer=consumer, broker=broker)

    factory = MagicMock(spec=DefaultAgentFactory)
    fake_instance = MagicMock()
    pipeline_calls: list[InputMessage] = []

    async def _mock_process(msg: InputMessage) -> None:
        pipeline_calls.append(msg)

    fake_instance.pipeline.process_message = AsyncMock(side_effect=_mock_process)
    fake_instance.stop = AsyncMock()
    fake_instance.descriptor.address = AgentAddress(kind="agent", name="worker")
    factory.create_agent = AsyncMock(return_value=fake_instance)

    pool = AgentPool(broker=broker, agent_factory=factory, agent_bus=bus)
    descriptor = AgentDescriptor(
        address=AgentAddress(kind="agent", name="worker"),
        context_strategy="persistent",
    )
    await pool.register_resident(descriptor)

    # 直接通过 bus 发送一条普通 agent_message
    envelope = AgentMessageEnvelope(
        payload={"content": "hello worker"},
        source=AgentAddress(kind="agent", name="subagent"),
        target=AgentAddress(kind="agent", name="worker"),
        message_type="agent_message",
        conversation_id="conv1",
        agent_session_id="conv1:worker",
    )
    await bus.send("conv1:worker", envelope)

    for _ in range(30):
        if pipeline_calls:
            break
        await asyncio.sleep(0.05)

    assert len(pipeline_calls) == 1
    assert pipeline_calls[0].content == "hello worker"
    assert pipeline_calls[0].metadata.get("message_type") == "agent_message"

    await pool.shutdown_all()
    await broker.stop()
