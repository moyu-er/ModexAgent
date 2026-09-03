"""Resolved-skill message metadata must survive broker transport."""

from __future__ import annotations

from modex_agent.core.message import ContentFormat
from modex_agent.core.session_id import SessionInfo
from modex_agent.messaging.broker import Address, AddressKind, BrokerMessage
from modex_agent.messaging.broker_bridge import build_input_broker_message
from modex_agent.messaging.models import InputMessage
from modex_agent.multi_agent.envelope import AgentMessageEnvelope


def _round_trip(message: InputMessage) -> tuple[BrokerMessage, InputMessage]:
    broker_message = build_input_broker_message(
        message,
        Address(kind=AddressKind.AGENT, name="main"),
    )
    restored_broker = BrokerMessage.model_validate_json(
        broker_message.model_dump_json()
    )
    envelope = AgentMessageEnvelope.from_broker_message(restored_broker)
    assert envelope is not None
    return broker_message, envelope.to_input_message(session=message.session)


def test_resolved_skill_metadata_survives_full_broker_round_trip() -> None:
    original = InputMessage(
        content="<user_input>review transport</user_input>",
        session=SessionInfo.from_str("s1.main"),
        content_format=ContentFormat.XML,
        truncatable_paths=["user_input"],
    )

    broker_message, reconstructed = _round_trip(original)

    assert broker_message.payload["content_format"] == "xml"
    assert broker_message.payload["truncatable_paths"] == ["user_input"]
    assert reconstructed.content_format is ContentFormat.XML
    assert tuple(reconstructed.truncatable_paths or ()) == ("user_input",)


def test_normal_message_omits_skill_metadata_and_reconstructs_defaults() -> None:
    original = InputMessage(
        content="hello",
        session=SessionInfo.from_str("s1.main"),
    )

    broker_message, reconstructed = _round_trip(original)

    assert "content_format" not in broker_message.payload
    assert "truncatable_paths" not in broker_message.payload
    assert reconstructed.content_format is None
    assert reconstructed.truncatable_paths is None
