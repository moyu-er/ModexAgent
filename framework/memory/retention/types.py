from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class RetentionPriority(StrEnum):
    SYSTEM_CRITICAL = "system_critical"
    USER_INPUT = "user_input"
    AGENT_INPUT = "agent_input"
    ASSISTANT_FINAL = "assistant_final"
    TOOL_CHAIN_STRUCTURE = "tool_chain_structure"
    TOOL_RESULT_RECENT = "tool_result_recent"
    ASSISTANT_INTERMEDIATE = "assistant_intermediate"
    TOOL_RESULT_OLD = "tool_result_old"
    LOW_VALUE_NOISE = "low_value_noise"


DEFAULT_PRIORITY_ORDER: tuple[RetentionPriority, ...] = (
    RetentionPriority.SYSTEM_CRITICAL,
    RetentionPriority.USER_INPUT,
    RetentionPriority.AGENT_INPUT,
    RetentionPriority.ASSISTANT_FINAL,
    RetentionPriority.TOOL_CHAIN_STRUCTURE,
    RetentionPriority.TOOL_RESULT_RECENT,
    RetentionPriority.ASSISTANT_INTERMEDIATE,
    RetentionPriority.TOOL_RESULT_OLD,
    RetentionPriority.LOW_VALUE_NOISE,
)


@dataclass(frozen=True)
class MessageRetentionDecision:
    priority: RetentionPriority
    rank: int
    anchor: bool
    reducible: bool
    summarizable: bool
    preserve_structure: bool
