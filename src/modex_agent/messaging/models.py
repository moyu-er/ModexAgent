"""Typed input, output, and approval messages crossing transport boundaries."""

from __future__ import annotations

from datetime import datetime
from enum import Enum, StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from modex_agent.core.media import Attachment
from modex_agent.core.message import ContentFormat
from modex_agent.core.session_id import SessionInfo

DEFAULT_CHANNEL = "default"
DEFAULT_SENDER_ID = "anonymous"
DEFAULT_RECIPIENT_ID = "agent"
DEFAULT_CHAT_ID = "default_session"


class MessageType(Enum):
    """Input/output message category."""

    TEXT = "text"
    COMMAND = "command"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    SYSTEM = "system"
    ERROR = "error"


class ReminderKind(StrEnum):
    """Source category for a framework-to-agent reminder."""

    AGENT_MESSAGE = "agent_message"
    PEER_MESSAGE = "peer_message"
    SUBAGENT_RESULT = "subagent_result"
    TODO_REORIENTATION = "todo_reorientation"


class OutputMessageType(StrEnum):
    """Output message category used by channel adapters."""

    TEXT = "text"
    IMAGE = "image"
    FILE = "file"
    ERROR = "error"
    APPROVAL_REQUEST = "approval_request"
    COMMAND_RESPONSE = "command_response"
    BUSY_NOTICE = "busy_notice"
    NOTICE = "notice"


class ApprovalAction(StrEnum):
    ALLOW = "allow"
    DENY = "deny"


class ApprovalDecisionInput(BaseModel):
    """Approve or deny decision transported with an input message."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool_call_id: str | None
    action: ApprovalAction


class InputMessage(BaseModel):
    """Normalized message accepted by an input adapter."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    content: str
    session: SessionInfo
    channel: str = DEFAULT_CHANNEL
    sender_id: str = DEFAULT_SENDER_ID
    chat_id: str = DEFAULT_CHAT_ID
    source: str = "unknown"
    msg_type: MessageType = MessageType.TEXT
    metadata: dict[str, Any] = Field(default_factory=dict)
    attachments: list[str] = Field(default_factory=list)
    timestamp: datetime = Field(default_factory=datetime.now)
    content_format: ContentFormat | None = None
    truncatable_paths: list[str] | None = None
    workspace: Path | None = None
    approval_decision: ApprovalDecisionInput | None = None
    attachments_resolved: list[Attachment] = Field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Serialize using the declared Pydantic schema."""
        return self.model_dump(mode="json")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> InputMessage:
        """Validate a serialized input message."""
        return cls.model_validate(data)


class OutputMessage(BaseModel):
    """Normalized message emitted through an output adapter."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    content: str
    session_id: str = "default"
    channel: str = DEFAULT_CHANNEL
    recipient_id: str = DEFAULT_RECIPIENT_ID
    chat_id: str = DEFAULT_CHAT_ID
    message_type: OutputMessageType = OutputMessageType.TEXT
    msg_type: MessageType = MessageType.TEXT
    reasoning: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    attachments: list[str] = Field(default_factory=list)
    timestamp: datetime = Field(default_factory=datetime.now)
    attachment_records: list[Attachment] = Field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Serialize using the declared Pydantic schema."""
        return self.model_dump(mode="json")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OutputMessage:
        """Validate a serialized output message."""
        return cls.model_validate(data)


class BrokerInputPayload(BaseModel):
    """Serialized ``InputMessage`` payload that crosses the message broker."""

    model_config = ConfigDict(frozen=True, extra="allow")

    content: str = ""
    session_id: str = ""
    agent_session_id: str = ""
    message_type: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    sender_id: str = DEFAULT_SENDER_ID
    chat_id: str = DEFAULT_CHAT_ID
    content_format: ContentFormat | None = None
    truncatable_paths: list[str] | None = None
    approval_decision: ApprovalDecisionInput | None = None
    attachments_resolved: list[Attachment] = Field(default_factory=list)
    workspace: str | None = None

    @classmethod
    def from_input_message(
        cls,
        message: InputMessage,
        *,
        agent_session_id: str,
        message_type: str = "",
    ) -> BrokerInputPayload:
        return cls(
            content=message.content,
            session_id=message.session.session_id_prefix,
            agent_session_id=agent_session_id,
            message_type=message_type,
            metadata=dict(message.metadata),
            sender_id=message.sender_id,
            chat_id=message.chat_id,
            content_format=message.content_format,
            truncatable_paths=message.truncatable_paths,
            approval_decision=message.approval_decision,
            attachments_resolved=list(message.attachments_resolved),
            workspace=str(message.workspace) if message.workspace is not None else None,
        )


class BrokerOutputPayload(BaseModel):
    """Validated output subset transported by a broker output adapter."""

    model_config = ConfigDict(frozen=True, extra="allow")

    content: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    session_id: str = "default"
    agent_session_id: str = "default"
    message_id: str = ""
    in_reply_to: str = ""

    @classmethod
    def from_output_message(
        cls,
        message: OutputMessage,
        *,
        session_id: str,
    ) -> BrokerOutputPayload:
        metadata = message.metadata
        return cls(
            content=message.content,
            metadata=metadata,
            session_id=str(metadata.get("session_id", session_id)),
            agent_session_id=str(metadata.get("agent_session_id", session_id)),
            message_id=str(metadata.get("message_id", "")),
            in_reply_to=str(metadata.get("in_reply_to", "")),
        )
