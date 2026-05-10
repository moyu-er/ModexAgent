"""Boundary policy: find a safe prune boundary that preserves tool-call chains."""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from enum import StrEnum
from typing import Any

from framework.memory.compaction.policy import MessageCompactionDecision
from framework.memory.compression.tool_chain import _find_tool_chain, _is_tool_call
from framework.memory.core.message import ChatMessage


class BoundaryPolicyName(StrEnum):
    """Configuration names for built-in compaction boundary policies."""

    TOOL_CHAIN = "tool_chain"
    PRIORITY_INPUT = "priority_input"
    USER_TURN_TOOL_CHAIN = "user_turn_tool_chain"


class BoundaryPolicy(ABC):
    """Determine a safe truncation boundary that never splits tool-call chains
    or cuts across messages marked ``KEEP_RAW``.
    """

    @abstractmethod
    def find_prune_boundary(
        self,
        messages: Sequence[ChatMessage | dict[str, Any]],
        decisions: Sequence[MessageCompactionDecision],
        target_prune_count: int,
    ) -> int:
        """Return the safe boundary index.

        ``messages[:boundary]`` are the messages that may be pruned;
        ``messages[boundary:]`` are retained.

        The returned boundary is guaranteed to be:
        - outside any incomplete tool-call chain.
        - before any ``KEEP_RAW`` message (i.e. all KEEP_RAW messages are retained).

        Args:
            messages: Full message list.
            decisions: Per-message compaction decisions (same length as *messages*).
            target_prune_count: Desired number of messages to prune from the head.

        Returns:
            Boundary index (0 .. len(messages)).
        """
        raise NotImplementedError


class ToolChainBoundaryPolicy(BoundaryPolicy):
    """Default boundary policy that protects tool-call chains and KEEP_RAW messages.

    The policy may return a boundary *smaller* than ``target_prune_count``
    when safety constraints require keeping additional tail messages (e.g.
    an incomplete tool-call chain would be split).  In that case the caller
    retains more messages than ``keep_recent`` requested.
    """

    def __init__(self, min_tail_keep: int = 1) -> None:
        self._min_tail_keep = min_tail_keep

    def find_prune_boundary(
        self,
        messages: Sequence[ChatMessage | dict[str, Any]],
        decisions: Sequence[MessageCompactionDecision],
        target_prune_count: int,
    ) -> int:
        if not messages:
            return 0

        n = len(messages)
        boundary = max(0, min(target_prune_count, n - self._min_tail_keep))

        # 1. Protect KEEP_RAW messages: shrink boundary so ALL KEEP_RAW messages
        # are in the remaining zone.
        keep_raw_indices = {
            i for i, d in enumerate(decisions) if d == MessageCompactionDecision.KEEP_RAW
        }
        if keep_raw_indices:
            first_keep_raw = min(keep_raw_indices)
            # If boundary would prune a KEEP_RAW message, shrink to before it
            if boundary > first_keep_raw:
                boundary = first_keep_raw

        # 2. Protect tool-call chains: do not let the boundary fall inside a chain.
        # If boundary would split a chain, shrink to before the chain start.
        i = 0
        while i < n:
            if _is_tool_call(messages[i]):
                chain = _find_tool_chain(messages, i)
                chain_end = max(chain)
                if i < boundary <= chain_end:
                    boundary = i
                    # After shrinking, restart scan from the top because earlier
                    # chains may now be affected.
                    i = 0
                    continue
                i = chain_end + 1
            else:
                i += 1

        # 3. Respect min_tail_keep
        boundary = min(boundary, n - self._min_tail_keep)

        return max(0, boundary)


class UserTurnToolChainBoundaryPolicy(ToolChainBoundaryPolicy):
    """Boundary policy that prefers user-turn boundaries.

    Extends ``ToolChainBoundaryPolicy`` with a preference for cutting
    *before* a user message or *after* a completed assistant final
    response (one without tool_calls), rather than at an arbitrary index.

    When no user-turn boundary is found within a reasonable lookahead,
    falls back to the parent tool-chain-protected boundary.
    """

    def __init__(self, min_tail_keep: int = 1, lookahead: int = 5) -> None:
        super().__init__(min_tail_keep=min_tail_keep)
        self._lookahead = lookahead

    def find_prune_boundary(
        self,
        messages: Sequence[ChatMessage | dict[str, Any]],
        decisions: Sequence[MessageCompactionDecision],
        target_prune_count: int,
    ) -> int:
        base = super().find_prune_boundary(messages, decisions, target_prune_count)
        if base <= 0:
            return base

        n = len(messages)
        may_move_forward = base >= target_prune_count

        # Try to find a user message boundary at or near the base index.
        # Move forward only when the parent did not shrink the boundary for safety.
        if may_move_forward:
            for offset in range(min(self._lookahead, n - base)):
                idx = base + offset
                role = (
                    messages[idx].get("role")
                    if isinstance(messages[idx], dict)
                    else getattr(messages[idx], "role", None)
                )
                if role in ("user", "User"):
                    return idx

        # Try to find a completed assistant final just before base
        # (assistant without tool_calls = end of a ReAct turn).
        for offset in range(min(self._lookahead, base)):
            idx = base - 1 - offset
            msg = messages[idx]
            role = msg.get("role") if isinstance(msg, dict) else getattr(msg, "role", None)
            has_tc = (
                bool(msg.get("tool_calls"))
                if isinstance(msg, dict)
                else bool(getattr(msg, "tool_calls", None))
            )
            if role in ("assistant", "Assistant") and not has_tc:
                return min(idx + 1, base)  # cut after this message

        return base


def create_boundary_policy(name: BoundaryPolicyName) -> BoundaryPolicy:
    """Create a built-in boundary policy from a typed configuration value."""

    if name is BoundaryPolicyName.TOOL_CHAIN:
        return ToolChainBoundaryPolicy()
    if name in (BoundaryPolicyName.PRIORITY_INPUT, BoundaryPolicyName.USER_TURN_TOOL_CHAIN):
        return UserTurnToolChainBoundaryPolicy()
    raise ValueError(f"Unsupported boundary policy: {name}")
