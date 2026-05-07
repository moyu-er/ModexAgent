from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from framework.memory.retention.types import DEFAULT_PRIORITY_ORDER, RetentionPriority


@dataclass(frozen=True)
class RetentionPolicyConfig:
    priority_order: tuple[RetentionPriority, ...] = DEFAULT_PRIORITY_ORDER
    recent_tool_result_count: int = 3
    min_recent_user_turns: int = 1
    min_recent_agent_turns: int = 1

    @classmethod
    def from_mapping(cls, raw: dict[str, Any] | None) -> RetentionPolicyConfig:
        if not raw:
            return cls()
        order_values = raw.get("priority_order")
        if order_values is None:
            priority_order = DEFAULT_PRIORITY_ORDER
        else:
            priority_order = tuple(RetentionPriority(str(item)) for item in order_values)
        recent_tool_result_count = int(raw.get("recent_tool_result_count", 3))
        anchors = raw.get("anchors", {})
        min_recent_user_turns = int(anchors.get("min_recent_user_turns", 1))
        min_recent_agent_turns = int(anchors.get("min_recent_agent_turns", 1))
        return cls(
            priority_order=priority_order,
            recent_tool_result_count=max(0, recent_tool_result_count),
            min_recent_user_turns=max(0, min_recent_user_turns),
            min_recent_agent_turns=max(0, min_recent_agent_turns),
        )
