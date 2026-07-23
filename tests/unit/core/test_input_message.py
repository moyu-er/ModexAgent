from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from modex_agent.approval.types import ApprovalAction
from modex_agent.approval.views import ApprovalDecisionInput
from modex_agent.core.message import ContentFormat
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.types import InputMessage, MessageType
from modex_agent.media.models import Attachment, AttachmentLocator, Kind


def test_input_message_defaults_no_approval_decision():
    msg = InputMessage(content="hi", session=SessionInfo.from_str("s.main"))
    assert msg.approval_decision is None


def test_input_message_carries_approval_decision():
    di = ApprovalDecisionInput(tool_call_id="c1", action=ApprovalAction.ALLOW)
    msg = InputMessage(content="", session=SessionInfo.from_str("s.main"), approval_decision=di)
    assert msg.approval_decision is di


def test_input_message_roundtrip():
    """to_dict -> from_dict preserves all InputMessage fields (TDD B5C).

    Covers the non-BaseModel fields (Path workspace, ApprovalDecisionInput
    dataclass) that require custom serialization in the to_dict/from_dict
    facades plus arbitrary_types_allowed on the model.
    """
    record = Attachment(
        id="att-1",
        kind=Kind.EXTRACTABLE_DOCUMENT,
        name="doc.pdf",
        mime="application/pdf",
        size=2048,
        path="media/doc.pdf",
        locator=AttachmentLocator.MEDIA,
    )
    ts = datetime(2025, 3, 20, 14, 0, 0, 654321)
    msg = InputMessage(
        content="hello world",
        session=SessionInfo(session_id="conv.main", agent_name="main"),
        channel="qq",
        sender_id="u1",
        chat_id="g1",
        source="qq",
        msg_type=MessageType.COMMAND,
        metadata={"k": "v"},
        attachments=["/tmp/a.png"],
        timestamp=ts,
        content_format=ContentFormat.XML,
        truncatable_paths=["/root/item"],
        workspace=Path("/tmp/workspace"),
        approval_decision=ApprovalDecisionInput(tool_call_id="c1", action=ApprovalAction.ALLOW),
        attachments_resolved=[record],
    )
    data = msg.to_dict()
    restored = InputMessage.from_dict(data)
    assert restored.content == msg.content
    assert restored.session == msg.session
    assert restored.channel == msg.channel
    assert restored.sender_id == msg.sender_id
    assert restored.chat_id == msg.chat_id
    assert restored.source == msg.source
    assert restored.msg_type == msg.msg_type
    assert restored.metadata == msg.metadata
    assert restored.attachments == msg.attachments
    assert restored.timestamp == msg.timestamp
    assert restored.content_format == msg.content_format
    assert restored.truncatable_paths == msg.truncatable_paths
    assert restored.workspace == msg.workspace
    assert restored.approval_decision == msg.approval_decision
    assert restored.attachments_resolved == msg.attachments_resolved


def test_input_message_frozen():
    """InputMessage is frozen — mutating a field raises ValidationError."""
    msg = InputMessage(content="x", session=SessionInfo.from_str("s1"))
    with pytest.raises(ValidationError):
        msg.content = "y"  # type: ignore[misc]

