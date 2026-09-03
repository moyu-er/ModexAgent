"""Provider error recovery for ReactLlmClient.

When the LLM provider raises a context-length / payload-too-large error,
this layer applies emergency compaction (drop middle messages, keep system +
recent tail) and lets the caller retry with the trimmed message list.

B6 note: ``ContextGovernance.apply`` still operates on ``list[dict[str, Any]]``
(the governance layer was not part of B6's scope).  ``attempt_recovery``
accepts ``list[ChatMessage]`` (the post-B6 caller type) and converts to/from
dict for the governance call.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict

from modex_agent.core.llm_struct import is_context_overflow_text
from modex_agent.core.message import ChatMessage
from modex_agent.memory.context_governance import ContextGovernance

if TYPE_CHECKING:
    from modex_agent.core.agent import AgentContext


def is_context_overflow_error(exc: Exception) -> bool:
    """Return *True* if *exc* indicates a context-length / payload-too-large error."""
    return is_context_overflow_text(str(exc).lower())


class ErrorRecoveryConfig(BaseModel):
    """Configuration for the error-recovery retry loop."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_context_overflow_retries: int = 2
    emergency_keep_messages: int = 10  # level 1: keep last 10
    emergency_keep_messages_level2: int = 5  # level 2: keep last 5


class EmergencyCompactionGovernance(ContextGovernance):
    """Drop all but the system message and the most recent *keep_messages* messages.

    The result always starts with a ``user`` message (after the optional system
    message) to satisfy the LLM API role-alternation requirement.
    """

    def __init__(self, keep_messages: int) -> None:
        self._keep_messages = keep_messages

    async def apply(
        self,
        messages: list[dict[str, Any]],
        ctx: AgentContext,
    ) -> list[dict[str, Any]]:
        if len(messages) <= self._keep_messages + 1:
            # Not enough to trim — return as-is (still a copy).
            return list(messages)

        system_msgs: list[dict[str, Any]] = []
        rest: list[dict[str, Any]] = []

        # Only index 0 can be the system message.
        if messages and messages[0].get("role") == "system":
            system_msgs = [messages[0]]
            rest = messages[1:]
        else:
            rest = list(messages)

        # Keep the last *keep_messages* non-system messages.
        tail = rest[-self._keep_messages:] if self._keep_messages > 0 else []

        # Ensure the kept list starts with a ``user`` message.
        # If no user message exists in the tail, prepend a minimal
        # synthetic user message to satisfy role-alternation at providers
        # that reject an assistant-leading message list.
        trimmed = tail
        while trimmed and trimmed[0].get("role") != "user":
            trimmed = trimmed[1:]
        if not trimmed:
            trimmed = [{"role": "user", "content": "Continue."}] + list(tail)

        return system_msgs + trimmed



class RecoveryAttempt(BaseModel):
    """Result of a single recovery attempt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    should_retry: bool
    trimmed_messages: list[ChatMessage] | None = None
    reason: str = ""


async def attempt_recovery(
    messages: list[ChatMessage],
    error: Exception,
    attempt_count: int,
    config: ErrorRecoveryConfig,
    ctx: AgentContext,
) -> RecoveryAttempt:
    """Decide whether to retry after *error* and produce trimmed messages if so.

    *attempt_count* starts at 0 (first overflow → level 1, second → level 2).
    """
    if is_context_overflow_error(error) and attempt_count < config.max_context_overflow_retries:
        if attempt_count == 0:
            keep = config.emergency_keep_messages
        else:
            keep = config.emergency_keep_messages_level2
        governance = EmergencyCompactionGovernance(keep_messages=keep)
        # Convert to dict for the governance layer (still dict-based).
        dict_messages = [m.to_dict() for m in messages]
        trimmed_dicts = await governance.apply(dict_messages, ctx)
        # Convert back to ChatMessage for the provider (post-B6).
        trimmed = [ChatMessage.coerce(d) for d in trimmed_dicts]
        return RecoveryAttempt(
            should_retry=True,
            trimmed_messages=trimmed,
            reason=f"Context overflow detected, applied emergency compaction level {attempt_count + 1}",
        )
    return RecoveryAttempt(should_retry=False, reason="No recovery available")
