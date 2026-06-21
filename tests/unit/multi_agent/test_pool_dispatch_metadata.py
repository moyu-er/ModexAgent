"""Tests that _dispatch_raw_broker_message preserves original adapter metadata.

Regression: _dispatch_raw_broker_message was constructing a new metadata dict
that only contained session_id and agent_session_id, DISCARDING all original
metadata from the payload — including user_id.  This caused IM users'
archive/knowledge to fall back to user_id="default", sharing scope with WebUI.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from framework.multi_agent.pool import AgentPool
from framework.multi_agent.state import AgentState


class _FakeBroker:
    async def consume(self, address):
        return None

    async def send_to(self, address, msg):
        pass


class TestDispatchMetadataPreservation:
    """_dispatch_raw_broker_message must pass adapter metadata to the pipeline."""

    @pytest.fixture
    async def pool(self):
        p = AgentPool(
            broker=_FakeBroker(),
            agent_factory=MagicMock(),
            enable_inbox_polling=False,
        )
        yield p
        await p.shutdown_all(timeout=0.1)

    @pytest.mark.asyncio
    async def test_raw_dispatch_preserves_user_id_from_metadata(self, pool):
        """QQ adapter sets user_id in metadata; the pipeline must receive it.

        Before fix: metadata was overwritten to only {session_id, agent_session_id},
        dropping user_id so all sessions fell back to "default" scope.
        """
        from framework.core.types import InputMessage
        from framework.multi_agent.address import AgentAddress
        from framework.messaging.broker import BrokerMessage

        # Create a mock pipeline that captures the InputMessage
        captured: list[InputMessage] = []

        class _FakePipeline:
            async def process_message(self, msg: InputMessage):
                captured.append(msg)

        # Create a descriptor for the agent
        from framework.multi_agent.descriptor import AgentDescriptor

        descriptor = AgentDescriptor(
            address=AgentAddress(kind="agent", name="main"),
        )

        instance = AsyncMock()
        instance.pipeline = _FakePipeline()
        instance.descriptor = descriptor

        pool._status["main"] = AgentState.IDLE

        # Simulate what PoolRouter._route_to_pool does: puts user_id in payload metadata
        broker_msg = BrokerMessage(
            payload={
                "content": "Hello from QQ",
                "session_id": "user12345",
                "metadata": {
                    "user_id": "user12345",
                    "session_id": "user12345",
                    "message_id": "msg-001",
                    "message_type": "agent_message",
                    "source_agent": "qq",
                },
            },
            sender=AgentAddress(kind="channel", name="qq"),
            recipient=AgentAddress(kind="agent", name="main"),
            headers={"session_id": "user12345"},
        )

        await pool._dispatch_raw_broker_message(instance, descriptor, broker_msg)

        assert len(captured) == 1, "Expected one InputMessage to reach the pipeline"
        msg = captured[0]
        metadata = msg.metadata or {}

        assert metadata.get("user_id") == "user12345", (
            f"user_id lost in dispatch! metadata={metadata}. "
            "Before fix: _dispatch_raw_broker_message discarded original metadata."
        )
        assert metadata.get("message_id") == "msg-001", (
            f"message_id lost in dispatch! metadata={metadata}"
        )
        assert metadata.get("session_id") == "user12345", (
            f"session_id should always be present. metadata={metadata}"
        )

    @pytest.mark.asyncio
    async def test_raw_dispatch_metadata_falls_back_gracefully(self, pool):
        """When payload has NO metadata dict, session_id is still set."""
        from framework.core.types import InputMessage
        from framework.multi_agent.address import AgentAddress
        from framework.messaging.broker import BrokerMessage
        from framework.multi_agent.descriptor import AgentDescriptor

        captured: list[InputMessage] = []

        class _FakePipeline:
            async def process_message(self, msg: InputMessage):
                captured.append(msg)

        descriptor = AgentDescriptor(
            address=AgentAddress(kind="agent", name="main"),
        )

        instance = AsyncMock()
        instance.pipeline = _FakePipeline()
        instance.descriptor = descriptor

        pool._status["main"] = AgentState.IDLE

        # Minimal payload — no metadata at all
        broker_msg = BrokerMessage(
            payload={
                "content": "bare message",
                "session_id": "conv-123",
            },
            sender=AgentAddress(kind="channel", name="webui"),
            recipient=AgentAddress(kind="agent", name="main"),
            headers={},
        )

        await pool._dispatch_raw_broker_message(instance, descriptor, broker_msg)

        assert len(captured) == 1
        metadata = captured[0].metadata or {}
        assert metadata.get("session_id") is not None, (
            "session_id should always be set even with no input metadata"
        )
        assert captured[0].content == "bare message"


class TestDispatchSourceAgentClassification:
    """source_agent must be set only for agent->agent traffic, not human turns.

    Regression: _dispatch_agent_message set source_agent from
    envelope.source.name unconditionally, so a webui/qq message (sender
    kind="channel") got source_agent="websocket", which ContextAssembler
    then classified as role=AGENT instead of role=USER. Only genuine
    agent->agent traffic (source.kind == "agent") should carry a source
    agent.
    """

    @pytest.fixture
    async def pool(self):
        p = AgentPool(
            broker=_FakeBroker(),
            agent_factory=MagicMock(),
            enable_inbox_polling=False,
        )
        yield p
        await p.shutdown_all(timeout=0.1)

    @pytest.mark.asyncio
    async def test_channel_source_does_not_set_source_agent(self, pool):
        from framework.core.types import InputMessage
        from framework.multi_agent.address import AgentAddress
        from framework.multi_agent.envelope import AgentMessageEnvelope
        from framework.multi_agent.descriptor import AgentDescriptor

        captured: list[InputMessage] = []

        class _FakePipeline:
            async def process_message(self, msg: InputMessage):
                captured.append(msg)

        descriptor = AgentDescriptor(address=AgentAddress(kind="agent", name="main"))
        instance = AsyncMock()
        instance.pipeline = _FakePipeline()
        instance.descriptor = descriptor
        pool._status["main"] = AgentState.IDLE

        # WebUI user message — sender is a CHANNEL, not an agent.
        envelope = AgentMessageEnvelope(
            payload={"content": "hello from webui"},
            source=AgentAddress(kind="channel", name="websocket"),
            target=AgentAddress(kind="agent", name="main"),
            message_type="agent_message",
            session_id="abc",
            agent_session_id="abc.main",
        )

        await pool._dispatch_agent_message(instance, envelope)

        assert len(captured) == 1
        metadata = captured[0].metadata or {}
        assert metadata.get("source_agent") is None, (
            f"channel/user turn must not carry source_agent (drives role=AGENT). "
            f"metadata={metadata}"
        )
        assert metadata.get("sender_agent") is None

    @pytest.mark.asyncio
    async def test_agent_source_still_sets_source_agent(self, pool):
        from framework.core.types import InputMessage
        from framework.multi_agent.address import AgentAddress
        from framework.multi_agent.envelope import AgentMessageEnvelope
        from framework.multi_agent.descriptor import AgentDescriptor

        captured: list[InputMessage] = []

        class _FakePipeline:
            async def process_message(self, msg: InputMessage):
                captured.append(msg)

        descriptor = AgentDescriptor(address=AgentAddress(kind="agent", name="coding"))
        instance = AsyncMock()
        instance.pipeline = _FakePipeline()
        instance.descriptor = descriptor
        pool._status["coding"] = AgentState.IDLE

        envelope = AgentMessageEnvelope(
            payload={"content": "subagent result"},
            source=AgentAddress(kind="agent", name="reviewer"),
            target=AgentAddress(kind="agent", name="coding"),
            message_type="subagent_result",
            session_id="abc",
            agent_session_id="abc.coding",
        )

        await pool._dispatch_agent_message(instance, envelope)

        assert len(captured) == 1
        metadata = captured[0].metadata or {}
        assert metadata.get("source_agent") == "reviewer"
        assert metadata.get("sender_agent") == "reviewer"


class TestDispatchSessionInfoResolution:
    """_dispatch_agent_message must preserve parent_session_id from registry/store.

    Regression: the dispatch path rebuilt SessionInfo via SessionInfo.from_str(),
    which cannot recover parent_session_id.  SubagentAutoSendHook then saw
    parent_session_id=None and silently skipped notifying the parent.
    """

    @pytest.fixture
    async def pool(self):
        p = AgentPool(
            broker=_FakeBroker(),
            agent_factory=MagicMock(),
            enable_inbox_polling=False,
        )
        yield p
        await p.shutdown_all(timeout=0.1)

    @pytest.mark.asyncio
    async def test_dispatch_uses_registry_session_info_with_parent(self, pool):
        """When registry has the child session with parent, dispatch keeps it."""
        from framework.core.session_id import SessionInfo
        from framework.core.session_registry import InMemorySessionRegistry
        from framework.core.types import InputMessage
        from framework.multi_agent.address import AgentAddress
        from framework.multi_agent.envelope import AgentMessageEnvelope
        from framework.multi_agent.descriptor import AgentDescriptor

        parent_sid = "abc.coding"
        child_sid = "abc.coding.reviewer.ee11"
        registry = InMemorySessionRegistry()
        await registry.register(
            SessionInfo(
                session_id=child_sid,
                agent_name="reviewer",
                parent_session_id=parent_sid,
                created_at=1,
                updated_at=1,
            )
        )
        pool._session_registry = registry

        captured: list[InputMessage] = []

        class _FakePipeline:
            async def process_message(self, msg: InputMessage):
                captured.append(msg)

        descriptor = AgentDescriptor(address=AgentAddress(kind="agent", name="reviewer"))
        instance = AsyncMock()
        instance.pipeline = _FakePipeline()
        instance.descriptor = descriptor
        pool._status["reviewer"] = AgentState.IDLE

        envelope = AgentMessageEnvelope(
            payload={"content": "subagent result"},
            source=AgentAddress(kind="agent", name="reviewer"),
            target=AgentAddress(kind="agent", name="coding"),
            message_type="subagent_result",
            session_id="abc",
            agent_session_id=child_sid,
            invocation_id="ee11",
        )

        await pool._dispatch_agent_message(instance, envelope)

        assert len(captured) == 1
        msg = captured[0]
        assert str(msg.session) == child_sid
        assert msg.session.parent_session_id == parent_sid, (
            f"parent_session_id lost in dispatch: {msg.session.parent_session_id}"
        )

    @pytest.mark.asyncio
    async def test_dispatch_falls_back_to_from_str_when_no_registry(self, pool):
        """When no registry/store is wired, dispatch falls back to from_str —
        parent_session_id is lost (the regression we guard against by wiring
        the registry in production)."""
        from framework.core.types import InputMessage
        from framework.multi_agent.address import AgentAddress
        from framework.multi_agent.envelope import AgentMessageEnvelope
        from framework.multi_agent.descriptor import AgentDescriptor

        captured: list[InputMessage] = []

        class _FakePipeline:
            async def process_message(self, msg: InputMessage):
                captured.append(msg)

        descriptor = AgentDescriptor(address=AgentAddress(kind="agent", name="coding"))
        instance = AsyncMock()
        instance.pipeline = _FakePipeline()
        instance.descriptor = descriptor
        pool._status["coding"] = AgentState.IDLE

        envelope = AgentMessageEnvelope(
            payload={"content": "hello"},
            source=AgentAddress(kind="agent", name="main"),
            target=AgentAddress(kind="agent", name="coding"),
            message_type="agent_message",
            session_id="abc",
            agent_session_id="abc.coding",
        )

        await pool._dispatch_agent_message(instance, envelope)

        assert len(captured) == 1
        assert str(captured[0].session) == "abc.coding"
        # Without a registry, parent_session_id cannot be recovered — this is
        # exactly why production must wire session_registry into the pool.
        assert captured[0].session.parent_session_id is None
        from framework.core.types import InputMessage
        from framework.multi_agent.address import AgentAddress
        from framework.multi_agent.envelope import AgentMessageEnvelope
        from framework.multi_agent.descriptor import AgentDescriptor

        captured: list[InputMessage] = []

        class _FakePipeline:
            async def process_message(self, msg: InputMessage):
                captured.append(msg)

        descriptor = AgentDescriptor(address=AgentAddress(kind="agent", name="coding"))
        instance = AsyncMock()
        instance.pipeline = _FakePipeline()
        instance.descriptor = descriptor
        pool._status["coding"] = AgentState.IDLE

        envelope = AgentMessageEnvelope(
            payload={"content": "hello"},
            source=AgentAddress(kind="agent", name="main"),
            target=AgentAddress(kind="agent", name="coding"),
            message_type="agent_message",
            session_id="abc",
            agent_session_id="abc.coding",
        )

        await pool._dispatch_agent_message(instance, envelope)

        assert len(captured) == 1
        assert str(captured[0].session) == "abc.coding"
