"""Capstone: two real pools each own their poller + inbox; no cross-talk.

After the poll-driven redesign (ADR-0015), every pool builds its OWN
``InboxPoller`` + ``InboxConsumer`` + ``LocalAgentMessageBus`` (Task 7) — there
is no shared bus. This test proves the isolation property directly with two
real ``AgentPool`` instances (main pool + coding pool), each with its own
broker / inbox server / bus / poller, each dispatching its own resident
subagent. A task_request addressed to pool A's subagent runs A's turn only;
pool B's poller and inbox stay empty — and vice versa.

This supersedes the old shared-bus fan-out test.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.integration

from modex_agent.core.session_id import SessionIdFactory, SessionInfo
from modex_agent.core.types import InputMessage
from modex_agent.messaging.broker_memory import InMemoryMessageBroker
from modex_agent.multi_agent import AgentDescriptor, SessionRetentionPolicy
from modex_agent.multi_agent.address import AgentAddress
from modex_agent.multi_agent.bus import LocalAgentMessageBus
from modex_agent.multi_agent.envelope import AgentMessageEnvelope
from modex_agent.multi_agent.inbox.consumer import InboxConsumer
from modex_agent.multi_agent.inbox_poller import InboxPoller
from modex_agent.multi_agent.inbox.producer import InboxProducer
from modex_agent.multi_agent.inbox.server_memory import InMemoryInboxServer
from modex_agent.multi_agent.pool import AgentPool
from modex_agent.multi_agent.state import AgentState


def _make_fake_instance(name: str) -> tuple[object, list[InputMessage]]:
    """A fake resident AgentInstance that records every processed InputMessage."""
    instance = MagicMock()
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
    """One pool's full poll-driven stack: broker + inbox server/bus + pool + poller."""

    def __init__(self, resident_name: str) -> None:
        self.resident_name = resident_name
        self.broker = InMemoryMessageBroker()
        self.server = InMemoryInboxServer()
        self.producer = InboxProducer(server=self.server)
        self.consumer = InboxConsumer(server=self.server)
        self.bus = LocalAgentMessageBus(
            producer=self.producer, consumer=self.consumer, broker=self.broker
        )
        self.session_factory = SessionIdFactory()

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
        )
        self.poller = InboxPoller(self.pool, interval=0.05)
        self.pool.attach_poller(self.poller)
        self.instance, self.calls = _make_fake_instance(resident_name)
        # Register the resident directly (bypass factory).
        self.pool._agents[resident_name] = self.instance
        self.pool._status[resident_name] = AgentState.IDLE

    async def start(self) -> None:
        await self.broker.start()
        self.pool.start_poller()

    async def stop(self) -> None:
        await self.pool.shutdown_all()
        await self.broker.stop()


@pytest.mark.asyncio
async def test_two_pools_isolate_inbox_dispatch() -> None:
    """A task_request for pool A's resident runs only in pool A; pool B stays idle."""
    main_bundle = _PoolBundle("worker")  # "main" pool with a resident named worker
    coding_bundle = _PoolBundle("coder")  # "coding" pool with a resident named coder

    await main_bundle.start()
    await coding_bundle.start()

    try:
        # Send a task_request to the MAIN pool's resident.
        main_sid = "conv-1.worker"
        main_envelope = AgentMessageEnvelope(
            payload={"content": "hello main-pool worker"},
            source=AgentAddress(kind="agent", name="main"),
            target=AgentAddress(kind="agent", name="worker"),
            message_type="task_request",
            session_id="conv-1",
            agent_session_id=main_sid,
        )
        await main_bundle.bus.send(main_sid, main_envelope)

        # Send a different task_request to the CODING pool's resident.
        coding_sid = "conv-2.coder"
        coding_envelope = AgentMessageEnvelope(
            payload={"content": "hello coding-pool coder"},
            source=AgentAddress(kind="agent", name="main"),
            target=AgentAddress(kind="agent", name="coder"),
            message_type="task_request",
            session_id="conv-2",
            agent_session_id=coding_sid,
        )
        await coding_bundle.bus.send(coding_sid, coding_envelope)

        # Wait for each poller to dispatch its own envelope (or timeout → fail).
        for _ in range(60):
            if main_bundle.calls and coding_bundle.calls:
                break
            await asyncio.sleep(0.05)

        # Each pool dispatched exactly its own message, to its own resident.
        assert len(main_bundle.calls) == 1, (
            f"main pool got {len(main_bundle.calls)} calls; expected 1"
        )
        assert main_bundle.calls[0].content == "hello main-pool worker"

        assert len(coding_bundle.calls) == 1, (
            f"coding pool got {len(coding_bundle.calls)} calls; expected 1"
        )
        assert coding_bundle.calls[0].content == "hello coding-pool coder"

        # No cross-talk: each pool's inbox is drained only of its own message.
        # After dispatch, neither pool has pending sessions for the OTHER's sid.
        main_pending = await main_bundle.pool.sessions_with_pending()
        coding_pending = await coding_bundle.pool.sessions_with_pending()
        assert coding_sid not in main_pending, (
            "main pool saw coding-pool's session — cross-talk!"
        )
        assert main_sid not in coding_pending, (
            "coding pool saw main-pool's session — cross-talk!"
        )
    finally:
        await main_bundle.stop()
        await coding_bundle.stop()
