"""Cross-pool peer-normal communication: pool A's main agent sends to pool B.

Two real pools with isolated inboxes/buses/pollers. Pool A's main agent sends
via a ``CommunicationTarget`` whose ``bus_ref`` points at pool B's bus. The
envelope lands in B's inbox on a session id derived from A's prefix, B's poller
registers the unseen session and starts a turn, and B's reply lands back in A's
inbox on the same prefix.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from modex_agent.core.agent import AgentCommKind, AgentContext
from modex_agent.core.session_id import SessionIdFactory, SessionInfo
from modex_agent.core.session_registry import InMemorySessionRegistry
from modex_agent.core.tool_manager import InMemoryToolManager
from modex_agent.core.types import InputMessage
from modex_agent.memory.history import ListMessageHistory
from modex_agent.messaging.broker_memory import InMemoryMessageBroker
from modex_agent.multi_agent import AgentDescriptor, SessionRetentionPolicy
from modex_agent.multi_agent.address import AgentAddress
from modex_agent.multi_agent.bus import LocalAgentMessageBus
from modex_agent.multi_agent.communication import AgentCommunicationService
from modex_agent.multi_agent.inbox.consumer import InboxConsumer
from modex_agent.multi_agent.inbox.producer import InboxProducer
from modex_agent.multi_agent.inbox.server_memory import InMemoryInboxServer
from modex_agent.multi_agent.inbox_poller import InboxPoller
from modex_agent.multi_agent.pool import AgentPool
from modex_agent.multi_agent.state import AgentState
from modex_agent.multi_agent.tools import CommunicationTarget, CommunicationTargetStore

pytestmark = pytest.mark.integration


def _make_fake_instance(name: str) -> tuple[Any, list[InputMessage]]:
    """A fake resident AgentInstance that records every processed InputMessage."""
    instance: Any = MagicMock()
    pipeline_calls: list[InputMessage] = []

    async def _process(msg: InputMessage) -> None:
        pipeline_calls.append(msg)

    instance.pipeline.process_message = AsyncMock(side_effect=_process)
    instance.pipeline.hook_runner = None
    instance.pipeline.hooks = []
    instance.pipeline.interceptor_chain = None
    instance.pipeline.turn_store = None
    instance.pipeline._user_interface = None
    instance.pipeline.command_processor = None
    instance.pipeline.governance = None
    instance.stop = AsyncMock()
    instance.descriptor = AgentDescriptor(
        address=AgentAddress(kind="agent", name=name),
        context_strategy="persistent",
    )
    return instance, pipeline_calls


class _PoolBundle:
    """One pool's full peer-ready stack: broker + inbox + bus + pool + poller + service."""

    def __init__(self, resident_name: str) -> None:
        self.resident_name = resident_name
        self.broker = InMemoryMessageBroker()
        self.server = InMemoryInboxServer()
        self.producer = InboxProducer(server=self.server)
        self.consumer = InboxConsumer(server=self.server)
        self.bus = LocalAgentMessageBus(
            producer=self.producer,
            consumer=self.consumer,
        )
        self.session_factory = SessionIdFactory()
        self.session_registry = InMemorySessionRegistry()
        self.target_store = CommunicationTargetStore()

        factory = MagicMock()
        factory.create_agent = AsyncMock()
        factory._default_hooks = []
        factory._default_hook_runner = None
        factory._default_interceptor_chain = None
        factory._default_turn_store = None
        factory._inbox_consumer = self.consumer

        self.pool = AgentPool(
            broker=self.broker,
            agent_factory=factory,
            agent_bus=self.bus,
            inbox_consumer=self.consumer,
            session_factory=self.session_factory,
            retention=SessionRetentionPolicy(),
            session_registry=self.session_registry,
        )
        self.poller = InboxPoller(self.pool, interval=0.05)
        self.pool.attach_poller(self.poller)
        self.instance, self.calls = _make_fake_instance(resident_name)
        self.pool._agents[resident_name] = self.instance
        self.pool._status[resident_name] = AgentState.IDLE

        self.service = AgentCommunicationService(
            source=AgentAddress(name=resident_name),
            broker=self.broker,
            registry=self.pool,
            agent_bus=self.bus,
            session_registry=self.session_registry,
            target_store=self.target_store,
        )

    async def start(self) -> None:
        await self.broker.start()
        self.pool.start_poller()

    async def stop(self) -> None:
        await self.pool.shutdown_all()
        await self.broker.stop()

    def make_context(self, session_id: str) -> AgentContext:
        return AgentContext(
            system_prompt="test",
            history=ListMessageHistory([]),
            tool_manager=InMemoryToolManager(),
            session=SessionInfo.from_str(session_id),
            comm_kind=AgentCommKind.NORMAL,
        )


@pytest.mark.asyncio
async def test_peer_normal_send_lands_in_peer_inbox_and_poller_registers_session() -> None:
    """A→B peer-normal send lands in B's inbox; B's poller registers the unseen session."""
    pool_a = _PoolBundle("mainA")
    pool_b = _PoolBundle("mainB")

    await pool_a.start()
    await pool_b.start()

    try:
        # Wire A to know B as a peer target (B's bus_ref is B's local bus).
        pool_a.target_store.add(
            CommunicationTarget(
                name="mainB",
                kind=AgentCommKind.NORMAL,
                pool_name="B",
                bus_ref=pool_b.bus,
                description="Pool B's main agent",
            )
        )
        pool_b.target_store.add(
            CommunicationTarget(
                name="mainA",
                kind=AgentCommKind.NORMAL,
                pool_name="A",
                bus_ref=pool_a.bus,
                description="Pool A's main agent",
            )
        )

        ctx_a = pool_a.make_context("convA.mainA")
        target_b = pool_a.target_store.get("mainB")
        assert target_b is not None

        ack = await pool_a.service.send_async(
            target=target_b,
            content="hello from A",
            invocation_id=None,
            context=ctx_a,
        )

        # A's ack hides the invocation_id from the sender.
        assert "invocation_id: " not in ack
        assert "Error" not in ack

        # The envelope lands in B's inbox on the A-prefix session, not in A's.
        b_pending = await pool_b.pool.sessions_with_pending()
        a_pending = await pool_a.pool.sessions_with_pending()
        assert "convA.mainB" in b_pending
        assert "convA.mainB" not in a_pending

        # B's poller starts the turn and registers the unseen session.
        for _ in range(60):
            if pool_b.calls:
                break
            await asyncio.sleep(0.05)
        assert len(pool_b.calls) == 1
        assert pool_b.calls[0].session.session_id == "convA.mainB"

        registered = await pool_b.session_registry.get("convA.mainB")
        assert registered is not None
        assert registered.session_id == "convA.mainB"
        assert registered.agent_name == "mainB"

        # B replies to A on the same prefix; the reply lands in A's inbox.
        ctx_b = pool_b.make_context("convA.mainB")
        target_a = pool_b.target_store.get("mainA")
        assert target_a is not None

        await pool_b.service.send_async(
            target=target_a,
            content="reply from B",
            invocation_id=None,
            context=ctx_b,
        )

        a_pending2 = await pool_a.pool.sessions_with_pending()
        assert "convA.mainA" in a_pending2
    finally:
        await pool_a.stop()
        await pool_b.stop()
