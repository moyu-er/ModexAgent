"""Integration test: verify main↔subagent communication within an AgentPool.

Exercises the full routing chain:
  main → AgentCommunicationService → bus → inbox poller → subagent pipeline
  subagent → AgentCommunicationService → bus → wakeup → main pipeline
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from framework.core.agent import AgentContext
from framework.core.session_id import SessionInfo, SessionIdFactory
from framework.core.emitter import AgentResult
from framework.core.session_registry import InMemorySessionRegistry
from framework.core.tool_manager import InMemoryToolManager
from framework.core.types import InputMessage
from framework.memory.history import ListMessageHistory
from framework.messaging.broker_memory import InMemoryMessageBroker
from framework.multi_agent.address import AgentAddress
from framework.multi_agent import AgentDescriptor, SessionRetentionPolicy
from framework.multi_agent.bus import LocalAgentMessageBus
from framework.multi_agent.comm_kind import AgentCommKind
from framework.multi_agent.communication import AgentCommunicationService
from framework.multi_agent.inbox.consumer import InboxConsumer
from framework.multi_agent.inbox.producer import InboxProducer
from framework.multi_agent.inbox.server_memory import InMemoryInboxServer
from framework.multi_agent.pool import AgentPool
from framework.multi_agent.state import AgentState


def _make_context(
    session_id: str = "conv-1",
    agent_name: str = "main",
    comm_kind: AgentCommKind = AgentCommKind.NORMAL,
    invocation_id: str | None = None,
) -> AgentContext:
    session_str = f"{session_id}.{agent_name}"
    if invocation_id:
        session_str = f"{session_str}.{invocation_id}"
    return AgentContext(
        system_prompt="test",
        history=ListMessageHistory([]),
        tool_manager=InMemoryToolManager(),
        session=SessionInfo.from_str(session_str),
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

    session_factory = SessionIdFactory()

    pool = AgentPool(
        broker=broker,
        agent_factory=factory,
        agent_bus=bus,
        inbox_consumer=consumer,
        enable_inbox_polling=True,
        inbox_poll_interval=0.1,  # fast polling for tests
        session_factory=session_factory,
        retention=SessionRetentionPolicy(),
    )

    return broker, bus, pool, session_factory, server


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
    broker, bus, pool, factory, server = await _create_pool_with_bus()

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
        session_factory=factory,
    )

    # Main sends to worker (new task → invocation_id="")
    ctx = _make_context(
        session_id="conv-1",
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
    broker, bus, pool, factory, server = await _create_pool_with_bus()

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
        session_factory=factory,
    )

    # Worker replies to main (invocation_id=None for NORMAL target)
    ctx = _make_context(
        session_id="conv-1",
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

    # The reply should be in main's inbox (session_id generated by SessionIdFactory)
    sessions = await server.list_sessions()
    main_sessions = [s for s in sessions if s.endswith(".main")]
    assert len(main_sessions) > 0, f"Main agent should have a pending session, found: {sessions}"
    main_session_id = main_sessions[0]
    assert await bus.has_pending(main_session_id), "Main agent should have pending reply in inbox"

    await pool.shutdown_all()
    await broker.stop()


@pytest.mark.asyncio
async def test_subagent_cannot_send_to_another_subagent():
    """Subagent-to-subagent communication is blocked."""
    broker, bus, pool, factory, server = await _create_pool_with_bus()

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
        session_factory=factory,
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


# ── Regression: subagent session must be registered with parent_session_id ──


@pytest.mark.asyncio
async def test_subagent_session_registered_with_parent_in_registry():
    """Regression: subagent session must be persisted with parent_session_id.

    When the communication service creates a subagent session (for a task
    sent to an already-registered resident subagent), it must register the
    session in the session_registry BEFORE dispatching the task.  Otherwise
    the WebUI frontend cannot identify which conversation a subagent belongs
    to, breaking the session tree.

    Bug: ``_send`` generic path (line ~1070-1082) created the subagent session
    with parent_session_id but never called ``registry.register()``, so the
    session_index (``.modex/session_index/``) was missing parent records for
    resident subagent sessions.
    """
    broker, bus, pool, factory, server = await _create_pool_with_bus()

    registry = InMemorySessionRegistry()
    pool._session_registry = registry

    main_inst = _make_fake_instance("main", AgentCommKind.NORMAL)[0]
    worker_inst = _make_fake_instance("worker", AgentCommKind.SUBAGENT)[0]

    pool._agents["main"] = main_inst
    pool._status["main"] = AgentState.IDLE
    pool._agents["worker"] = worker_inst
    pool._status["worker"] = AgentState.IDLE

    main_service = AgentCommunicationService(
        source=AgentAddress(name="main"),
        broker=broker,
        registry=pool,
        agent_bus=bus,
        session_factory=factory,
        session_registry=registry,
    )

    ctx = _make_context(
        session_id="conv-parent",
        agent_name="main",
        comm_kind=AgentCommKind.NORMAL,
    )
    # Use _send directly — it returns AgentSendResult with .session_id
    # and creates the session + registers it in the registry before dispatch.
    result = await main_service._send(
        target_agent="worker",
        content="please do something",
        invocation_id="task-1",  # non-empty → general _send path
        context=ctx,
        async_mode=True,
    )

    assert result is not None
    assert result.error is None, f"Send failed: {result.error}"

    # After _send returns, the session MUST be registered with parent_session_id.
    # The communication service creates subagent sessions via factory with
    # parent_session_id set, so they must be persisted to the registry.
    session = await registry.get(result.session_id)
    assert session is not None, (
        f"Subagent session {result.session_id!r} must be registered in session_registry"
    )
    assert session.parent_session_id is not None, (
        f"Subagent session {result.session_id!r} must have parent_session_id, "
        f"got None (missing parent → frontend session tree broken)"
    )

    await pool.shutdown_all()
    await broker.stop()
