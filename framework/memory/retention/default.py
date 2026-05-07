from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from framework.core.types import MessageRole
from framework.memory.core.message import ChatMessage
from framework.memory.core.scope import MemoryContext
from framework.memory.retention.config import RetentionPolicyConfig
from framework.memory.retention.policy import MessageRetentionPolicy
from framework.memory.retention.types import MessageRetentionDecision, RetentionPriority


class DefaultMessageRetentionPolicy(MessageRetentionPolicy):
    """Default role-based retention policy.

    Human user input ranks above agent input. Agent input ranks above assistant
    and tool process content. Configurable ordering keeps future extension
    points out of compression and governance algorithms.
    """

    @staticmethod
    def _msg_to_dict(message: ChatMessage | dict[str, Any]) -> dict[str, Any]:
        if isinstance(message, ChatMessage):
            return message.to_dict()
        return dict(message)

    def __init__(
        self,
        *,
        priority_order: Sequence[RetentionPriority] | None = None,
        recent_tool_result_count: int = 3,
        min_recent_user_turns: int = 1,
        min_recent_agent_turns: int = 1,
    ) -> None:
        config = RetentionPolicyConfig(
            priority_order=tuple(priority_order) if priority_order else RetentionPolicyConfig().priority_order,
            recent_tool_result_count=max(0, recent_tool_result_count),
            min_recent_user_turns=max(0, min_recent_user_turns),
            min_recent_agent_turns=max(0, min_recent_agent_turns),
        )
        self._config = config
        self._rank = {priority: idx for idx, priority in enumerate(config.priority_order)}
        self._tool_indices_cache_key: int | None = None
        self._tool_indices_cache: list[int] = []

    @classmethod
    def from_config(cls, raw: dict[str, Any] | None) -> DefaultMessageRetentionPolicy:
        config = RetentionPolicyConfig.from_mapping(raw)
        return cls(
            priority_order=config.priority_order,
            recent_tool_result_count=config.recent_tool_result_count,
            min_recent_user_turns=config.min_recent_user_turns,
            min_recent_agent_turns=config.min_recent_agent_turns,
        )

    @property
    def min_recent_user_turns(self) -> int:
        return self._config.min_recent_user_turns

    @property
    def min_recent_agent_turns(self) -> int:
        return self._config.min_recent_agent_turns

    def decide(
        self,
        message: ChatMessage | dict[str, Any],
        *,
        index: int,
        messages: Sequence[ChatMessage | dict[str, Any]],
        context: MemoryContext | None = None,
    ) -> MessageRetentionDecision:
        _ = context
        msg = self._msg_to_dict(message)
        role = str(msg.get("role", ""))
        priority = self._priority_for(msg, index=index, messages=messages)
        return MessageRetentionDecision(
            priority=priority,
            rank=self._rank.get(priority, len(self._rank)),
            anchor=priority in {RetentionPriority.USER_INPUT, RetentionPriority.AGENT_INPUT},
            reducible=priority not in {RetentionPriority.SYSTEM_CRITICAL, RetentionPriority.TOOL_CHAIN_STRUCTURE},
            summarizable=priority != RetentionPriority.SYSTEM_CRITICAL,
            preserve_structure=role in {
                str(MessageRole.SYSTEM),
                str(MessageRole.ASSISTANT),
                str(MessageRole.TOOL),
            },
        )

    def _priority_for(
        self,
        msg: dict[str, Any],
        *,
        index: int,
        messages: Sequence[ChatMessage | dict[str, Any]],
    ) -> RetentionPriority:
        role = str(msg.get("role", ""))
        if role == str(MessageRole.SYSTEM):
            return RetentionPriority.SYSTEM_CRITICAL
        if role == str(MessageRole.USER):
            return RetentionPriority.USER_INPUT
        if role == str(MessageRole.AGENT):
            return RetentionPriority.AGENT_INPUT
        if role == str(MessageRole.ASSISTANT) and msg.get("tool_calls"):
            return RetentionPriority.TOOL_CHAIN_STRUCTURE
        if role == str(MessageRole.TOOL):
            return self._tool_priority(index=index, messages=messages)
        if role == str(MessageRole.ASSISTANT):
            return RetentionPriority.ASSISTANT_FINAL
        return RetentionPriority.LOW_VALUE_NOISE

    def _tool_priority(
        self,
        *,
        index: int,
        messages: Sequence[ChatMessage | dict[str, Any]],
    ) -> RetentionPriority:
        if self._config.recent_tool_result_count <= 0:
            return RetentionPriority.TOOL_RESULT_OLD
        # Cache tool indices per messages sequence to avoid O(n^2) scans
        cache_key = id(messages)
        if getattr(self, "_tool_indices_cache_key", None) != cache_key:
            self._tool_indices_cache = [
                idx
                for idx, item in enumerate(messages)
                if str(self._msg_to_dict(item).get("role", "")) == str(MessageRole.TOOL)
            ]
            self._tool_indices_cache_key = cache_key
        recent = set(self._tool_indices_cache[-self._config.recent_tool_result_count :])
        if index in recent:
            return RetentionPriority.TOOL_RESULT_RECENT
        return RetentionPriority.TOOL_RESULT_OLD
