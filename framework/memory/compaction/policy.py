"""Message compaction policy: decide per-message fate during memory pruning."""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from enum import StrEnum
from typing import Any

from framework.core.types import MessageRole
from framework.memory.core.message import ChatMessage
from framework.memory.core.scope import MemoryContext


class MessageCompactionDecision(StrEnum):
    """Per-message decision for how it should be handled during compaction."""

    KEEP_RAW = "keep_raw"
    """Retain in short-term; do not prune this message."""

    SUMMARIZE = "summarize"
    """Eligible for summarization; content may be condensed into a summary."""

    DROP_FROM_SUMMARY = "drop_from_summary"
    """Prune without including in the LLM-generated summary.

    The message may still be archived raw (e.g. tool outputs) but will not
    appear in the semantic summary text.
    """

    ARCHIVE_RAW = "archive_raw"
    """Archive the raw message without summarization."""


class MessageCompactionPolicy(ABC):
    """Decide how each message should be handled during compaction.

    Implementations inspect role, tool_calls, content, and other metadata
    to classify messages into one of the MessageCompactionDecision values.
    """

    @abstractmethod
    def decide(
        self,
        message: ChatMessage | dict[str, Any],
        context: MemoryContext,
        reason: str,
    ) -> MessageCompactionDecision:
        """Return the compaction decision for a single message.

        Args:
            message: The message to classify.
            context: Current memory context.
            reason: Trigger reason (e.g. ``"token_pressure"``, ``"idle_compact"``).

        Returns:
            A MessageCompactionDecision value.
        """
        raise NotImplementedError

    def decide_all(
        self,
        messages: Sequence[ChatMessage | dict[str, Any]],
        context: MemoryContext,
        reason: str,
    ) -> list[MessageCompactionDecision]:
        """Classify an entire message sequence.

        Default implementation calls ``decide()`` for each message.
        Subclasses may override for batch/lookahead optimizations.
        """
        return [self.decide(m, context, reason) for m in messages]


def _msg_role(message: ChatMessage | dict[str, Any]) -> str:
    if isinstance(message, ChatMessage):
        return message.role or ""
    return message.get("role") or ""


def _has_tool_calls(message: ChatMessage | dict[str, Any]) -> bool:
    if isinstance(message, ChatMessage):
        return bool(message.tool_calls)
    return bool(message.get("tool_calls"))


class ConservativeCompactionPolicy(MessageCompactionPolicy):
    """Default conservative policy.

    - ``user`` and plain ``assistant`` messages: ``SUMMARIZE``
    - ``assistant`` with ``tool_calls``: ``KEEP_RAW`` (protect tool-call chain)
    - ``tool`` messages: ``DROP_FROM_SUMMARY`` (archive raw but don't summarize)
    - ``system`` / ``developer``: ``KEEP_RAW`` (never prune into user history)
    """

    # Tools whose results are considered high-value and may be summarized.
    # If a tool name matches, its result message decision becomes SUMMARIZE.
    high_value_tools: set[str]

    def __init__(self, high_value_tools: set[str] | None = None) -> None:
        self.high_value_tools = high_value_tools or set()

    def decide(
        self,
        message: ChatMessage | dict[str, Any],
        context: MemoryContext,
        reason: str,
    ) -> MessageCompactionDecision:
        _ = context, reason
        role = _msg_role(message)

        if role == MessageRole.SYSTEM:
            return MessageCompactionDecision.KEEP_RAW

        if role == MessageRole.ASSISTANT and _has_tool_calls(message):
            return MessageCompactionDecision.KEEP_RAW

        if role == MessageRole.TOOL:
            name = message.get("name") if isinstance(message, dict) else message.name
            if name and name in self.high_value_tools:
                return MessageCompactionDecision.SUMMARIZE
            return MessageCompactionDecision.DROP_FROM_SUMMARY

        # user and plain assistant
        return MessageCompactionDecision.SUMMARIZE


class KeepAllCompactionPolicy(MessageCompactionPolicy):
    """Keep every message raw. Useful for debugging or very small contexts."""

    def decide(
        self,
        message: ChatMessage | dict[str, Any],
        context: MemoryContext,
        reason: str,
    ) -> MessageCompactionDecision:
        _ = message, context, reason
        return MessageCompactionDecision.KEEP_RAW


class SemanticToolCompactionPolicy(MessageCompactionPolicy):
    """Compatibility alias for ``ConservativeCompactionPolicy``.

    This class exists so that configs referencing ``"semantic"`` policy
    do not break.  It delegates entirely to ``ConservativeCompactionPolicy``
    and will be removed once all callers migrate to the canonical name.
    """

    def __init__(self, high_value_tools: set[str] | None = None) -> None:
        self._fallback = ConservativeCompactionPolicy(high_value_tools=high_value_tools)

    def decide(
        self,
        message: ChatMessage | dict[str, Any],
        context: MemoryContext,
        reason: str,
    ) -> MessageCompactionDecision:
        return self._fallback.decide(message, context, reason)
