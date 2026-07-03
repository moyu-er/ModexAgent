"""Inbox 消息类型定义。"""

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


@dataclass(frozen=True)
class InboxMessage:
    """Inbox 中传递和持久化的单条消息。"""

    session_id: str
    source: str
    content: str
    message_type: str
    message_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    metadata: dict[str, Any] = field(default_factory=dict)
