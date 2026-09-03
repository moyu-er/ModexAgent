"""Resolved attachments must survive the broker transport.

The webui/IM inbound attachment path-reference injection (ADR-0013 §10,
mechanism B) reads ``InputMessage.attachments_resolved`` at preprocess time.
In production the InputMessage crosses the message broker (input adapter ->
PoolRouter._route_to_pool / broker bridge -> broker -> pool
``_dispatch_agent_message`` -> ``input_message_from_dispatch_envelope`` ->
``process_message`` -> ``preprocess``). If the broker hop drops
``attachments_resolved``, the agent never perceives the uploaded file: the
``[Attachment: ...]`` line is never injected, so the agent answers as if no
file was sent. This is the same field-drift class that once dropped
``approval_decision``.

These tests pin the transport contract: the publish side carries
``attachments_resolved`` in the broker payload, and the dispatch side
reconstructs it.
"""

from __future__ import annotations

from pathlib import Path

from modex_agent.core.media import Attachment, AttachmentLocator, Kind
from modex_agent.core.session_id import SessionInfo
from modex_agent.messaging.broker import Address
from modex_agent.messaging.broker_bridge import build_input_broker_message
from modex_agent.messaging.models import InputMessage
from modex_agent.multi_agent.envelope import AgentMessageEnvelope
from modex_agent.multi_agent.pool import input_message_from_dispatch_envelope


def _session() -> SessionInfo:
    return SessionInfo.from_str("s1.main")


def _attachment(name: str = "photo.png") -> Attachment:
    return Attachment(
        id="abc123",
        kind=Kind.IMAGE,
        name=name,
        mime="image/png",
        size=72,
        path="data/media/main/uploads/s1_main/abc123",
        locator=AttachmentLocator.MEDIA,
    )


def test_attachment_round_trips_through_dict() -> None:
    """Attachment serializes to/from a plain dict (broker-safe)."""
    att = _attachment()
    assert Attachment.from_dict(att.to_dict()) == att


def test_build_input_broker_message_carries_attachments_resolved() -> None:
    msg = InputMessage(
        content="look at this",
        session=_session(),
        attachments_resolved=[_attachment("a.png"), _attachment("b.png")],
    )
    broker_msg = build_input_broker_message(msg, Address(kind="agent", name="main"))
    raw = broker_msg.payload["attachments_resolved"]
    assert isinstance(raw, list) and len(raw) == 2
    assert raw[0]["name"] == "a.png"


def test_build_input_broker_message_omits_attachments_when_absent() -> None:
    msg = InputMessage(content="hello", session=_session())
    broker_msg = build_input_broker_message(msg, Address(kind="agent", name="main"))
    # No attachments_resolved -> empty list (the field defaults to []; the
    # dispatch side treats [] the same as absence).
    assert broker_msg.payload.get("attachments_resolved", []) == []


def test_attachments_resolved_survive_full_broker_round_trip() -> None:
    """Regression: the resolved attachments reach preprocess after the broker hop."""
    original = InputMessage(
        content="look",
        session=_session(),
        attachments_resolved=[_attachment("cat.png")],
    )
    broker_msg = build_input_broker_message(original, Address(kind="agent", name="main"))
    envelope = AgentMessageEnvelope.from_broker_message(broker_msg)
    assert envelope is not None

    reconstructed = input_message_from_dispatch_envelope(envelope, session=_session())
    assert len(reconstructed.attachments_resolved) == 1
    rec = reconstructed.attachments_resolved[0]
    assert rec == _attachment("cat.png")
    assert rec.locator is AttachmentLocator.MEDIA


def test_normal_message_round_trips_without_attachments() -> None:
    original = InputMessage(content="hi there", session=_session())
    broker_msg = build_input_broker_message(original, Address(kind="agent", name="main"))
    envelope = AgentMessageEnvelope.from_broker_message(broker_msg)
    assert envelope is not None

    reconstructed = input_message_from_dispatch_envelope(envelope, session=_session())
    assert reconstructed.attachments_resolved == []
    assert reconstructed.content == "hi there"


def test_workspace_round_trips_through_broker_transport() -> None:
    original = InputMessage(
        content="work here",
        session=_session(),
        workspace=Path("D:/projects/demo"),
    )

    broker_msg = build_input_broker_message(original, Address(kind="agent", name="main"))
    envelope = AgentMessageEnvelope.from_broker_message(broker_msg)
    assert envelope is not None

    reconstructed = input_message_from_dispatch_envelope(envelope, session=_session())

    assert reconstructed.workspace == original.workspace
