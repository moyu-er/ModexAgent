from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol

from framework.memory.core.message import ChatMessage
from framework.memory.core.scope import MemoryContext
from framework.memory.retention.types import MessageRetentionDecision


class MessageRetentionPolicy(Protocol):
    def decide(
        self,
        message: ChatMessage | dict[str, Any],
        *,
        index: int,
        messages: Sequence[ChatMessage | dict[str, Any]],
        context: MemoryContext | None = None,
    ) -> MessageRetentionDecision:
        """Classify one message for retention and governance planning."""
        ...
