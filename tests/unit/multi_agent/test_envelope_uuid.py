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
            session_id="conv-1",
            agent_session_id="conv-1.main",
        )
        assert envelope.invocation_id is None

    def test_envelope_uuid_preserved_explicit(self) -> None:
        envelope = AgentMessageEnvelope(
            payload={"content": "hello"},
            source=AgentAddress(name="main"),
            target=AgentAddress(name="office-expert"),
            session_id="conv-1",
            agent_session_id="conv-1.office-expert.task-1",
            invocation_id="task-1",
        )
        assert envelope.invocation_id == "task-1"

    def test_broker_message_round_trip_preserves_uuid(self) -> None:
        envelope = AgentMessageEnvelope(
            payload={"content": "hello"},
            source=AgentAddress(name="main"),
            target=AgentAddress(name="office-expert"),
            session_id="conv-1",
            agent_session_id="conv-1.office-expert.task-1",
            invocation_id="task-1",
        )
        broker_msg = envelope.to_broker_message()
        assert broker_msg.headers.get("invocation_id") == "task-1"

        restored = AgentMessageEnvelope.from_broker_message(broker_msg)
        assert restored is not None
        assert restored.invocation_id == "task-1"

    def test_broker_message_round_trip_without_uuid(self) -> None:
        envelope = AgentMessageEnvelope(
            payload={"content": "hello"},
            source=AgentAddress(name="main"),
            target=AgentAddress(name="reviewer"),
            session_id="conv-1",
            agent_session_id="conv-1.reviewer",
        )
        broker_msg = envelope.to_broker_message()
        assert "invocation_id" not in broker_msg.headers

        restored = AgentMessageEnvelope.from_broker_message(broker_msg)
        assert restored is not None
        assert restored.invocation_id is None

    def test_invocation_id_excluded_from_metadata_round_trip(self) -> None:
        envelope = AgentMessageEnvelope(
            payload={"content": "hello"},
            source=AgentAddress(name="sub"),
            session_id="conv-1",
            agent_session_id="conv-1.main",
            invocation_id="abc123",
            metadata={"extra": "value"},
        )
        broker_msg = envelope.to_broker_message()
        restored = AgentMessageEnvelope.from_broker_message(broker_msg)
        assert restored is not None
        assert restored.invocation_id == "abc123"
        assert "invocation_id" not in restored.metadata
