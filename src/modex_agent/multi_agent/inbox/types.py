"""Inbox 消息类型定义。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field

SESSION_WORK_METADATA_KEY: Final = "session_tree_work"


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


class SessionWork(BaseModel):
    """Reserved inbox messages, retired only after their receiver processes them."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pending: tuple[InboxMessage, ...] = ()
