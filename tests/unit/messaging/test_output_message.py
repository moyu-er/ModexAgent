from __future__ import annotations

from datetime import datetime

import pytest
from pydantic import ValidationError

from modex_agent.core.media import Attachment, AttachmentLocator, Kind
from modex_agent.messaging.models import (
    DEFAULT_CHANNEL,
    MessageType,
    OutputMessage,
    OutputMessageType,
)


def test_output_message_roundtrip():
    """to_dict -> from_dict preserves all OutputMessage fields (TDD B5C)."""
    record = Attachment(
        id="att-1",
        kind=Kind.IMAGE,
        name="pic.png",
        mime="image/png",
        size=1024,
        path="media/pic.png",
        locator=AttachmentLocator.MEDIA,
    )
    ts = datetime(2025, 1, 15, 10, 30, 0, 123456)
    msg = OutputMessage(
        content="hello world",
        session_id="s.main",
        channel=DEFAULT_CHANNEL,
        recipient_id="u1",
        chat_id="g1",
        message_type=OutputMessageType.IMAGE,
        msg_type=MessageType.COMMAND,
        reasoning="thinking...",
        metadata={"k": "v"},
        attachments=["/tmp/a.png"],
        timestamp=ts,
        attachment_records=[record],
    )
    data = msg.to_dict()
    restored = OutputMessage.from_dict(data)
    assert restored.content == msg.content
    assert restored.session_id == msg.session_id
    assert restored.channel == msg.channel
    assert restored.recipient_id == msg.recipient_id
    assert restored.chat_id == msg.chat_id
    assert restored.message_type == msg.message_type
    assert restored.msg_type == msg.msg_type
    assert restored.reasoning == msg.reasoning
    assert restored.metadata == msg.metadata
    assert restored.attachments == msg.attachments
    assert restored.timestamp == msg.timestamp
    assert restored.attachment_records == msg.attachment_records


def test_output_message_frozen():
    """OutputMessage is frozen — mutating a field raises ValidationError."""
    msg = OutputMessage(content="x")
    with pytest.raises(ValidationError):
        msg.content = "y"  # type: ignore[misc]
