"""Integration test: verify main↔subagent communication within an AgentPool.

Exercises the full routing chain:
  main → AgentCommunicationService → bus → inbox poller → subagent pipeline
  subagent → AgentCommunicationService → bus → wakeup → main pipeline
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from framework.core.agent import AgentContext
from framework.core.session_id import SessionId
from framework.core.emitter import AgentResult
from framework.core.tool_manager import InMemoryToolManager
from framework.core.types import InputMessage
from framework.memory.history import ListMessageHistory
from framework.messaging.broker_memory import InMemoryMessageBroker
from framework.multi_agent import AgentAddress, AgentDescriptor, SessionRetentionPolicy
from framework.multi_agent.bus import LocalAgentMessageBus
from framework.multi_agent.comm_kind import AgentCommKind
from framework.multi_agent.communication import AgentCommunicationService
from framework.multi_agent.inbox.consumer import InboxConsumer
from framework.multi_agent.inbox.producer import InboxProducer
from framework.multi_agent.inbox.server_memory import InMemoryInboxServer
from framework.multi_agent.pool import AgentPool
from framework.multi_agent.session_id import DefaultSessionIdStrategy
from framework.multi_agent.state import AgentState


def _make_context(
    conversation_id: str = "conv-1",
    agent_name: str = "main",
    comm_kind: AgentCommKind = AgentCommKind.NORMAL,
    invocation_id: str | None = None,
) -> AgentContext:
    session_str = f"{conversation_id}.{agent_name}"
    if invocation_id:
        session_str = f"{session_str}.{invocation_id}"
    return AgentContext(
        system_prompt="test",
        history=ListMessageHistory([]),
        tool_manager=InMemoryToolManager(),
        session=SessionId.from_str(session_str),
        comm_kind=comm_kind,
    )


async def _create_pool_with_bus():
    """Create a pool with broker, bus, and inbox infrastructure."""
    broker = InMemoryMessageBroker()
    await broker.start()

    server = InMemoryInboxServer()
    producer = InboxProducer(server=server)
    consumer = InboxConsumer(server=server)
    bus = LocalAgentMessageBus(producer=producer, consumer=consumer, broker=broker)

    factory = MagicMock()
    factory.create_agent = AsyncMock()
    factory._default_hooks = []
    factory._default_hook_runner = None
    factory._default_interceptor_chain = None
    factory._default_turn_store = None
    factory._inbox_consumer = consumer

    strategy = DefaultSessionIdStrategy(main_agent_name="main")

    pool = AgentPool(
        broker=broker,
        agent_factory=factory,
        agent_bus=bus,
        inbox_consumer=consumer,
        enable_inbox_polling=True,
        inbox_poll_interval=0.1,  # fast polling for tests
        session_strategy=strategy,
        retention=SessionRetentionPolicy(),
    )

    return broker, bus, pool, strategy


def _make_fake_instance(name: str, comm_kind: AgentCommKind):
    """Create a fake AgentInstance with a mock pipeline."""
    instance = MagicMock()
    pipeline_calls: list[InputMessage] = []

    async def _process(msg: InputMessage) -> AgentResult:
        pipeline_calls.append(msg)
        return AgentResult(content=f"{name} processed", stop_reason="completed")

    instance.pipeline.process_message = AsyncMock(side_effect=_process)
    instance.pipeline.hook_runner = None
    instance.pipeline.hooks = []
    instance.pipeline.interceptor_chain = None
    instance.pipeline.turn_store = None
    instance.pipeline._approval_workspace = None
    instance.pipeline._user_interface = None
    instance.pipeline.command_processor = None
    instance.pipeline.governance = None
    instance.stop = AsyncMock()
    instance.context_manager = MagicMock()
    instance.context_manager.clear = AsyncMock()
    descriptor = AgentDescriptor(
        address=AgentAddress(kind="agent", name=name),
        comm_kind=comm_kind,
        context_strategy="persistent",
    )
    instance.descriptor = descriptor
    return instance, pipeline_calls


@pytest.mark.asyncio
async def test_main_sends_to_subagent_via_communication_service():
    """Main agent sends to subagent via AgentCommunicationService → bus → pool dispatch."""
    broker, bus, pool, strategy = await _create_pool_with_bus()

    # Register main (NORMAL) and worker (SUBAGENT) agents
    main_inst = _make_fake_instance("main", AgentCommKind.NORMAL)[0]
    worker_inst = _make_fake_instance("worker", AgentCommKind.SUBAGENT)[0]

    pool._agents["main"] = main_inst
    pool._status["main"] = AgentState.IDLE
    pool._agents["worker"] = worker_inst
    pool._status["worker"] = AgentState.IDLE

    # Create communication service for main agent
    main_service = AgentCommunicationService(
        source=AgentAddress(name="main"),
        broker=broker,
        registry=pool,
        agent_bus=bus,
        session_strategy=strategy,
    )

    # Main sends to worker (new task → invocation_id="")
    ctx = _make_context(
        conversation_id="conv-1",
        agent_name="main",
        comm_kind=AgentCommKind.NORMAL,
    )
    result = await main_service.send_async(
        target_agent="worker",
        content="do some work",
        invocation_id="",
        context=ctx,
    )

    # Should succeed
    assert "worker" in result
    assert "Error" not in result

    await pool.shutdown_all()
    await broker.stop()


@pytest.mark.asyncio
async def test_subagent_replies_to_main_via_communication_service():
    """Subagent sends reply to main via AgentCommunicationService → bus → wakeup."""
    broker, bus, pool, strategy = await _create_pool_with_bus()

    main_inst = _make_fake_instance("main", AgentCommKind.NORMAL)[0]
    worker_inst = _make_fake_instance("worker", AgentCommKind.SUBAGENT)[0]

    pool._agents["main"] = main_inst
    pool._status["main"] = AgentState.IDLE
    pool._agents["worker"] = worker_inst
    pool._status["worker"] = AgentState.IDLE

    # Create communication service for subagent
    worker_service = AgentCommunicationService(
        source=AgentAddress(name="worker"),
        broker=broker,
        registry=pool,
        agent_bus=bus,
        session_strategy=strategy,
    )

    # Worker replies to main (invocation_id=None for NORMAL target)
    ctx = _make_context(
        conversation_id="conv-1",
        agent_name="worker",
        comm_kind=AgentCommKind.SUBAGENT,
        invocation_id="task-abc1",
    )
    result = await worker_service.send_async(
        target_agent="main",
        content="task completed",
        invocation_id=None,
        context=ctx,
    )

    assert "main" in result
    assert "Error" not in result

    # The reply should be in main's inbox: "conv-1.main" (dot separator, filesystem-safe)
    has_pending = await bus.has_pending("conv-1.main")
    assert has_pending, "Main agent should have pending reply in inbox"

    await pool.shutdown_all()
    await broker.stop()


@pytest.mark.asyncio
async def test_subagent_cannot_send_to_another_subagent():
    """Subagent-to-subagent communication is blocked."""
    broker, bus, pool, strategy = await _create_pool_with_bus()

    pool._agents["main"] = _make_fake_instance("main", AgentCommKind.NORMAL)[0]
    pool._agents["worker_a"] = _make_fake_instance("worker_a", AgentCommKind.SUBAGENT)[0]
    pool._agents["worker_b"] = _make_fake_instance("worker_b", AgentCommKind.SUBAGENT)[0]
    from framework.multi_agent.state import AgentState
    pool._status["main"] = AgentState.IDLE
    pool._status["worker_a"] = AgentState.IDLE
    pool._status["worker_b"] = AgentState.IDLE

    service = AgentCommunicationService(
        source=AgentAddress(name="worker_a"),
        broker=broker,
        registry=pool,
        agent_bus=bus,
        session_strategy=strategy,
    )

    ctx = _make_context(
        agent_name="worker_a",
        comm_kind=AgentCommKind.SUBAGENT,
        invocation_id="task-1",
    )
    result = await service.send_async(
        target_agent="worker_b",
        content="help me",
        invocation_id="",
        context=ctx,
    )

    assert "Error" in result
    assert "subagent" in result.lower()

    await pool.shutdown_all()
    await broker.stop()
