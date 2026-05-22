"""Tests for first-class UUID on AgentMessageEnvelope serialization round-trips."""

from __future__ import annotations

import pytest

from framework.multi_agent.address import AgentAddress
from framework.multi_agent.envelope import AgentMessageEnvelope


class TestEnvelopeUUID:
    def test_envelope_uuid_defaults_to_none(self) -> None:
        envelope = AgentMessageEnvelope(
            payload={"content": "hello"},
            source=AgentAddress(name="main"),
            conversation_id="conv-1",
            agent_session_id="conv-1:main",
        )
        assert envelope.uuid is None

    def test_envelope_uuid_preserved_explicit(self) -> None:
        envelope = AgentMessageEnvelope(
            payload={"content": "hello"},
            source=AgentAddress(name="main"),
            target=AgentAddress(name="office-expert"),
            conversation_id="conv-1",
            agent_session_id="conv-1:office-expert:task-1",
            uuid="task-1",
        )
        assert envelope.uuid == "task-1"

    def test_broker_message_round_trip_preserves_uuid(self) -> None:
        envelope = AgentMessageEnvelope(
            payload={"content": "hello"},
            source=AgentAddress(name="main"),
            target=AgentAddress(name="office-expert"),
            conversation_id="conv-1",
            agent_session_id="conv-1:office-expert:task-1",
            uuid="task-1",
        )
        broker_msg = envelope.to_broker_message()
        assert broker_msg.headers.get("uuid") == "task-1"

        restored = AgentMessageEnvelope.from_broker_message(broker_msg)
        assert restored is not None
        assert restored.uuid == "task-1"

    def test_broker_message_round_trip_without_uuid(self) -> None:
        envelope = AgentMessageEnvelope(
            payload={"content": "hello"},
            source=AgentAddress(name="main"),
            target=AgentAddress(name="reviewer"),
            conversation_id="conv-1",
            agent_session_id="conv-1:reviewer",
        )
        broker_msg = envelope.to_broker_message()
        assert "uuid" not in broker_msg.headers

        restored = AgentMessageEnvelope.from_broker_message(broker_msg)
        assert restored is not None
        assert restored.uuid is None

    def test_uuid_excluded_from_metadata_round_trip(self) -> None:
        envelope = AgentMessageEnvelope(
            payload={"content": "hello"},
            source=AgentAddress(name="sub"),
            conversation_id="conv-1",
            agent_session_id="conv-1:main",
            uuid="abc123",
            metadata={"extra": "value"},
        )
        broker_msg = envelope.to_broker_message()
        restored = AgentMessageEnvelope.from_broker_message(broker_msg)
        assert restored is not None
        assert restored.uuid == "abc123"
        assert "uuid" not in restored.metadata
