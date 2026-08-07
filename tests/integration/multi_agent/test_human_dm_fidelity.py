"""Capstone: a human DM's full InputMessage round-trips the broker boundary.

C2 fidelity guard (poll-driven redesign): a human/webui/approval InputMessage
carrying ``approval_decision`` (a real ``ApprovalDecisionInput``) AND
``attachments_resolved`` (real ``Attachment`` records) is serialized by
``AgentPool.submit_input`` into the envelope payload, then reconstructed by
``input_message_from_dispatch_envelope`` with BOTH fields intact — not
flattened to an empty user turn (the regression that lost webui approve/deny
decisions and path-injected attachments crossing the broker).

Uses a real AgentPool + InboxPoller; a stub pipeline captures the
reconstructed InputMessage so we assert on the real reconstruction path.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.integration

from modex_agent.approval.views import ApprovalDecisionInput
from modex_agent.approval.types import ApprovalAction
from modex_agent.core.session_id import SessionIdFactory, SessionInfo
from modex_agent.core.types import InputMessage
from modex_agent.media.models import Attachment, AttachmentLocator, Kind
from modex_agent.messaging.broker_memory import InMemoryMessageBroker
from modex_agent.multi_agent import SessionRetentionPolicy
from modex_agent.multi_agent.address import AgentAddress
from modex_agent.multi_agent.bus import LocalAgentMessageBus
from modex_agent.multi_agent.inbox.consumer import InboxConsumer
from modex_agent.multi_agent.inbox_poller import InboxPoller
from modex_agent.multi_agent.inbox.producer import InboxProducer
from modex_agent.multi_agent.inbox.server_memory import InMemoryInboxServer
from modex_agent.multi_agent.pool import AgentPool
from modex_agent.multi_agent.state import AgentState


def _make_capturing_instance(name: str) -> tuple[object, list[InputMessage]]:
    """A fake resident whose pipeline.process_message records each InputMessage."""
    instance = MagicMock()
    captured: list[InputMessage] = []

    async def _process(msg: InputMessage) -> None:
        captured.append(msg)

    instance.pipeline.process_message = AsyncMock(side_effect=_process)
    instance.pipeline.hook_runner = None
    instance.pipeline.hooks = []
    instance.pipeline.interceptor_chain = None
    instance.pipeline.turn_store = None
    instance.pipeline._user_interface = None
    instance.pipeline.command_processor = None
    instance.pipeline.governance = None
    instance.stop = AsyncMock()
    instance.descriptor = MagicMock(
        address=AgentAddress(kind="agent", name=name),
    )
    return instance, captured


@pytest.mark.asyncio
async def test_human_dm_round_trips_approval_and_attachments() -> None:
    """A full InputMessage reconstructs with approval_decision + attachments intact."""
    broker = InMemoryMessageBroker()
    await broker.start()

    server = InMemoryInboxServer()
    producer = InboxProducer(server=server)
    consumer = InboxConsumer(server=server)
    bus = LocalAgentMessageBus(producer=producer, consumer=consumer)

    factory = MagicMock()
    factory.create_agent = AsyncMock()
    factory._default_hooks = []
    factory._default_hook_runner = None
    factory._default_interceptor_chain = None
    factory._default_turn_store = None
    factory._inbox_consumer = consumer

    pool = AgentPool(
        broker=broker,
        agent_factory=factory,
        agent_bus=bus,
        inbox_consumer=consumer,
        session_factory=SessionIdFactory(),
        retention=SessionRetentionPolicy(),
    )
    poller = InboxPoller(pool, interval=0.05)
    pool.attach_poller(poller)

    # Register a resident "main" agent with a capturing pipeline.
    instance, captured = _make_capturing_instance("main")
    pool._agents["main"] = instance
    pool._status["main"] = AgentState.IDLE

    pool.start_poller()

    try:
        # Build a REAL InputMessage carrying both an approval decision and a
        # resolved attachment — the full C2 payload surface.
        decision = ApprovalDecisionInput(tool_call_id="call_abc123", action=ApprovalAction.ALLOW)
        attachment = Attachment(
            id="att-1",
            kind=Kind.IMAGE,
            name="photo.png",
            mime="image/png",
            size=2048,
            path="/ws/.media/att-1/photo.png",
            locator=AttachmentLocator.MEDIA,
        )
        session_info = SessionInfo.from_str("conv-DM.main")
        original = InputMessage(
            content="approved with attachment",
            session=session_info,
            approval_decision=decision,
            attachments_resolved=[attachment],
            source="webui",
        )

        agent_session_id = "conv-DM.main"
        await pool.submit_input(agent_session_id, original)

        # Wait for the poller to dispatch the envelope → reconstructed InputMessage.
        for _ in range(60):
            if captured:
                break
            await asyncio.sleep(0.05)

        assert len(captured) == 1, (
            f"pipeline saw {len(captured)} messages; expected the one submitted"
        )
        reconstructed = captured[0]

        # Content survives.
        assert reconstructed.content == "approved with attachment"

        # approval_decision survives the broker round-trip (C2 guard).
        assert reconstructed.approval_decision is not None, (
            "approval_decision was lost across submit_input → dispatch_envelope"
        )
        assert reconstructed.approval_decision.tool_call_id == "call_abc123"
        assert reconstructed.approval_decision.action is ApprovalAction.ALLOW

        # attachments_resolved survives the broker round-trip (C2 guard).
        assert reconstructed.attachments_resolved, (
            "attachments_resolved was lost across submit_input → dispatch_envelope"
        )
        assert len(reconstructed.attachments_resolved) == 1
        rt = reconstructed.attachments_resolved[0]
        assert rt.id == "att-1"
        assert rt.kind is Kind.IMAGE
        assert rt.name == "photo.png"
        assert rt.mime == "image/png"
        assert rt.size == 2048
        assert rt.path == "/ws/.media/att-1/photo.png"
        assert rt.locator is AttachmentLocator.MEDIA
    finally:
        await pool.shutdown_all()
        await broker.stop()
