"""Integration test: verify main↔subagent communication within an AgentPool.

Exercises the full routing chain:
  main → AgentCommunicationService → bus → inbox poller → subagent pipeline
  subagent → AgentCommunicationService → bus → wakeup → main pipeline
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from modex_agent.core.agent import AgentContext
from modex_agent.core.session_id import SessionInfo, SessionIdFactory
from modex_agent.core.emitter import AgentResult
from modex_agent.core.session_registry import InMemorySessionRegistry
from modex_agent.core.types import InputMessage
from modex_agent.memory.history import ListMessageHistory
from modex_agent.messaging.broker_memory import InMemoryMessageBroker
from modex_agent.multi_agent.address import AgentAddress
from modex_agent.multi_agent import AgentDescriptor, SessionRetentionPolicy
from modex_agent.multi_agent.bus import LocalAgentMessageBus
from modex_agent.multi_agent.comm_kind import AgentCommKind
from modex_agent.multi_agent.communication import AgentCommunicationService
from modex_agent.multi_agent.inbox.consumer import InboxConsumer
from modex_agent.multi_agent.inbox.producer import InboxProducer
from modex_agent.multi_agent.inbox.server_memory import InMemoryInboxServer
from modex_agent.multi_agent.inbox_poller import InboxPoller
from modex_agent.multi_agent.pool import AgentPool
from modex_agent.multi_agent.session_tree.manager import SessionTreeManager
from modex_agent.multi_agent.session_tree.store_node import InMemoryTreeNodeStore
from modex_agent.multi_agent.session_tree.store_track import InMemoryMessageTrackStore
from modex_agent.multi_agent.session_tree.store_tree import InMemorySessionTreeStore
from modex_agent.multi_agent.state import AgentState
from modex_agent.multi_agent.tools import CommunicationTarget
from modex_agent.tools.manager import InMemoryToolManager


class _StubPoller(InboxPoller):
    """Real InboxPoller subclass that needs no AgentPool."""

    def __init__(self) -> None:
        self.signaled = False

    def signal_wakeup(self) -> None:
        self.signaled = True


def _tgt(name: str, kind: AgentCommKind) -> CommunicationTarget:
    return CommunicationTarget(name=name, kind=kind)


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
    bus = LocalAgentMessageBus(producer=producer, consumer=consumer)

    tree_manager = SessionTreeManager(
        tree_store=InMemorySessionTreeStore(),
        node_store=InMemoryTreeNodeStore(),
        track_store=InMemoryMessageTrackStore(),
        bus=bus,
        poller=_StubPoller(),
        pool_name="test-pool",
        workspace_root="/tmp",
        session_registry=InMemorySessionRegistry(),
    )
    consumer.set_on_consumed(tree_manager.on_consumed)

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
        inbox_consumer=consumer,
        session_factory=session_factory,
        retention=SessionRetentionPolicy(),
    )
    pool._tree = tree_manager

    return broker, bus, pool, session_factory, server, tree_manager


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
    broker, bus, pool, factory, server, tree_manager = await _create_pool_with_bus()

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
        registry=pool,
        tree=tree_manager,
        session_factory=factory,
    )

    # Main sends to worker (new task → invocation_id="")
    ctx = _make_context(
        session_id="conv-1",
        agent_name="main",
        comm_kind=AgentCommKind.NORMAL,
    )
    result = await main_service.send_async(
        target=_tgt("worker", AgentCommKind.SUBAGENT),
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
    broker, bus, pool, factory, server, tree_manager = await _create_pool_with_bus()

    main_inst = _make_fake_instance("main", AgentCommKind.NORMAL)[0]
    worker_inst = _make_fake_instance("worker", AgentCommKind.SUBAGENT)[0]

    pool._agents["main"] = main_inst
    pool._status["main"] = AgentState.IDLE
    pool._agents["worker"] = worker_inst
    pool._status["worker"] = AgentState.IDLE

    # Create communication service for subagent
    worker_service = AgentCommunicationService(
        source=AgentAddress(name="worker"),
        registry=pool,
        tree=tree_manager,
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
        target=_tgt("main", AgentCommKind.NORMAL),
        content="done",
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
    assert main_session_id in await bus.sessions_with_pending(), (
        "Main agent should have pending reply in inbox"
    )

    await pool.shutdown_all()
    await broker.stop()


@pytest.mark.asyncio
async def test_subagent_cannot_send_to_another_subagent():
    """Subagent-to-subagent communication is blocked."""
    broker, bus, pool, factory, server, tree_manager = await _create_pool_with_bus()

    pool._agents["main"] = _make_fake_instance("main", AgentCommKind.NORMAL)[0]
    pool._agents["worker_a"] = _make_fake_instance("worker_a", AgentCommKind.SUBAGENT)[0]
    pool._agents["worker_b"] = _make_fake_instance("worker_b", AgentCommKind.SUBAGENT)[0]
    pool._status["main"] = AgentState.IDLE
    pool._status["worker_a"] = AgentState.IDLE
    pool._status["worker_b"] = AgentState.IDLE

    service = AgentCommunicationService(
        source=AgentAddress(name="worker_a"),
        registry=pool,
        tree=tree_manager,
        session_factory=factory,
    )

    ctx = _make_context(
        agent_name="worker_a",
        comm_kind=AgentCommKind.SUBAGENT,
        invocation_id="task-1",
    )
    result = await service.send_async(
        target=_tgt("worker_b", AgentCommKind.SUBAGENT),
        content="hello worker b",
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
    broker, bus, pool, factory, server, tree_manager = await _create_pool_with_bus()

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
        registry=pool,
        tree=tree_manager,
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
        target=_tgt("worker", AgentCommKind.SUBAGENT),
        content="please do something",
        invocation_id="task-1",  # non-empty → general _send path
        context=ctx,
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
