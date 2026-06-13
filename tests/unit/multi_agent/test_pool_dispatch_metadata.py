"""Tests that _dispatch_raw_broker_message preserves original adapter metadata.

Regression: _dispatch_raw_broker_message was constructing a new metadata dict
that only contained conversation_id and agent_session_id, DISCARDING all original
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

        Before fix: metadata was overwritten to only {conversation_id, agent_session_id},
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
                    "conversation_id": "user12345",
                    "message_id": "msg-001",
                    "message_type": "agent_message",
                    "source_agent": "qq",
                },
            },
            sender=AgentAddress(kind="channel", name="qq"),
            recipient=AgentAddress(kind="agent", name="main"),
            headers={"conversation_id": "user12345"},
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
        assert metadata.get("conversation_id") == "user12345", (
            f"conversation_id should always be present. metadata={metadata}"
        )

    @pytest.mark.asyncio
    async def test_raw_dispatch_metadata_falls_back_gracefully(self, pool):
        """When payload has NO metadata dict, conversation_id is still set."""
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
        assert metadata.get("conversation_id") is not None, (
            "conversation_id should always be set even with no input metadata"
        )
        assert captured[0].content == "bare message"
