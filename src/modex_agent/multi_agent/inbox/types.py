"""Inbox 消息类型定义。"""

import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class InboxMessage(BaseModel):
    """Inbox 中传递和持久化的单条消息。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: str
    source: str
    content: str
    message_type: str
    message_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    metadata: dict[str, Any] = Field(default_factory=dict)
