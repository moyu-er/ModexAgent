"""Priority-aware keep planner for persistent memory compression."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from framework.core.types import MessageRole
from framework.memory.compaction.policy import MessageCompactionDecision
from framework.memory.core.models import CompressionReason
from framework.memory.retention import MessageRetentionDecision, RetentionPriority
from framework.memory.utils import estimate_token_count


class KeepPlanReason(StrEnum):
    """Reason codes describing how a keep plan was chosen."""

    LATEST_USER_ANCHOR = "latest_user_anchor"
    LATEST_AGENT_ANCHOR = "latest_agent_anchor"
    BUDGET_SUFFIX = "budget_suffix"
    BUDGET_SUFFIX_WITHOUT_ANCHOR = "budget_suffix_without_anchor"
    NO_SAFE_BOUNDARY = "no_safe_boundary"
    EMPTY = "empty"


@dataclass(frozen=True)
class CompressionBudget:
    reason: CompressionReason
    max_keep_messages: int | None
    max_keep_tokens: int | None


@dataclass(frozen=True)
class CompressionKeepPlan:
    keep_start_index: int
    keep_messages: list[dict[str, Any]]
    pruned_messages: list[dict[str, Any]]
    pruned_indices: list[int]
    reason: KeepPlanReason
    within_budget: bool


class CompressionKeepPlanner(Protocol):
    def plan_keep_set(
        self,
        messages: Sequence[dict[str, Any]],
        decisions: Sequence[MessageCompactionDecision],
        retention: Sequence[MessageRetentionDecision],
        budget: CompressionBudget,
    ) -> CompressionKeepPlan:
        """Plan a legal retained suffix for compression."""
        ...


class PriorityCompressionKeepPlanner:
    """Contiguous-suffix planner with user-before-agent priority."""

    def plan_keep_set(
        self,
        messages: Sequence[dict[str, Any]],
        decisions: Sequence[MessageCompactionDecision],
        retention: Sequence[MessageRetentionDecision],
        budget: CompressionBudget,
    ) -> CompressionKeepPlan:
        _ = decisions
        total = len(messages)
        if total == 0:
            return CompressionKeepPlan(0, [], [], [], KeepPlanReason.EMPTY, True)

        max_keep = budget.max_keep_messages if budget.max_keep_messages is not None else total
        max_keep = max(1, min(max_keep, total))

        budget_start = self._budget_suffix_start(messages, budget, max_keep)
        candidates = self._candidate_starts(messages, retention, budget_start)
        for start, reason in candidates:
            keep = [dict(m) for m in messages[start:]]
            if self._fits(keep, budget) and self._legal_suffix(keep):
                return CompressionKeepPlan(
                    start,
                    keep,
                    [dict(m) for m in messages[:start]],
                    list(range(start)),
                    reason,
                    True,
                )
            reduced = self._reduce_suffix_after_anchor(messages, start, budget)
            if reduced is not None:
                keep_indices, keep_messages = reduced
                pruned_indices = [idx for idx in range(total) if idx not in keep_indices]
                return CompressionKeepPlan(
                    start,
                    keep_messages,
                    [dict(messages[idx]) for idx in pruned_indices],
                    pruned_indices,
                    reason,
                    True,
                )

        newest_start = budget_start
        while newest_start < total and not self._legal_suffix([dict(m) for m in messages[newest_start:]]):
            newest_start += 1
        if newest_start >= total:
            return CompressionKeepPlan(
                total,
                [],
                [dict(m) for m in messages],
                list(range(total)),
                KeepPlanReason.NO_SAFE_BOUNDARY,
                False,
            )
        keep = [dict(m) for m in messages[newest_start:]]
        return CompressionKeepPlan(
            newest_start,
            keep,
            [dict(m) for m in messages[:newest_start]],
            list(range(newest_start)),
            KeepPlanReason.BUDGET_SUFFIX_WITHOUT_ANCHOR,
            self._fits(keep, budget),
        )

    def _reduce_suffix_after_anchor(
        self,
        messages: Sequence[dict[str, Any]],
        anchor_start: int,
        budget: CompressionBudget,
    ) -> tuple[set[int], list[dict[str, Any]]] | None:
        keep_indices = set(range(anchor_start, len(messages)))
        if not keep_indices:
            return None
        protected_indices = {anchor_start}
        drop_groups = self._drop_groups_after_anchor(messages, anchor_start)

        while True:
            keep_messages = [dict(messages[idx]) for idx in sorted(keep_indices)]
            if self._fits(keep_messages, budget) and self._legal_sequence(keep_messages):
                return keep_indices, keep_messages
            if not drop_groups:
                return None
            group = drop_groups.pop(0)
            if group & protected_indices:
                continue
            keep_indices.difference_update(group)

    def _drop_groups_after_anchor(
        self,
        messages: Sequence[dict[str, Any]],
        anchor_start: int,
    ) -> list[set[int]]:
        groups: list[set[int]] = []
        idx = anchor_start + 1
        while idx < len(messages):
            msg = messages[idx]
            role = msg.get("role")
            if role == str(MessageRole.ASSISTANT) and msg.get("tool_calls"):
                group = {idx}
                tool_ids = {
                    str(call.get("id"))
                    for call in msg.get("tool_calls") or []
                    if isinstance(call, dict) and call.get("id")
                }
                scan = idx + 1
                while scan < len(messages):
                    next_msg = messages[scan]
                    if next_msg.get("role") != str(MessageRole.TOOL):
                        break
                    if str(next_msg.get("tool_call_id", "")) in tool_ids:
                        group.add(scan)
                    scan += 1
                groups.append(group)
                idx = max(group) + 1
                continue
            if role in {str(MessageRole.ASSISTANT), str(MessageRole.TOOL)}:
                groups.append({idx})
            idx += 1
        return groups

    def _candidate_starts(
        self,
        messages: Sequence[dict[str, Any]],
        retention: Sequence[MessageRetentionDecision],
        budget_start: int,
    ) -> list[tuple[int, KeepPlanReason]]:
        total = len(messages)
        min_start = max(0, min(budget_start, total - 1))
        user_indices = self._priority_indices(retention, RetentionPriority.USER_INPUT)
        agent_indices = self._priority_indices(retention, RetentionPriority.AGENT_INPUT)
        candidates: list[tuple[int, KeepPlanReason]] = []

        # Prefer the earliest user anchor that can fit. This preserves as many
        # user-started ReAct rounds as the hard budget allows. If a candidate
        # does not fit, plan_keep_set will try later anchors or reduce process
        # assistant/tool groups after the anchor.
        for idx in user_indices:
            if idx >= min_start:
                candidates.append((idx, KeepPlanReason.LATEST_USER_ANCHOR))
        for idx in agent_indices:
            if idx >= min_start:
                candidates.append((idx, KeepPlanReason.LATEST_AGENT_ANCHOR))

        # If all anchors are older than the raw budget suffix, still try the
        # most recent user/agent with reduction before falling back to budget
        # suffix. This supports "keep the current task input, trim process".
        if user_indices and all(idx < min_start for idx in user_indices):
            candidates.append((user_indices[-1], KeepPlanReason.LATEST_USER_ANCHOR))
        if agent_indices and all(idx < min_start for idx in agent_indices):
            candidates.append((agent_indices[-1], KeepPlanReason.LATEST_AGENT_ANCHOR))

        candidates.append((min_start, KeepPlanReason.BUDGET_SUFFIX))
        seen: set[int] = set()
        deduped: list[tuple[int, KeepPlanReason]] = []
        for start, reason in candidates:
            if start not in seen:
                seen.add(start)
                deduped.append((start, reason))
        return deduped

    @staticmethod
    def _budget_suffix_start(
        messages: Sequence[dict[str, Any]],
        budget: CompressionBudget,
        max_keep: int,
    ) -> int:
        if budget.max_keep_messages is not None:
            return max(0, len(messages) - max_keep)
        if budget.max_keep_tokens is None:
            return 0

        accumulated = 0
        start = len(messages) - 1
        for idx in range(len(messages) - 1, -1, -1):
            msg_tokens = estimate_token_count([messages[idx]])
            if idx < len(messages) - 1 and accumulated + msg_tokens > budget.max_keep_tokens:
                break
            accumulated += msg_tokens
            start = idx
        return max(0, start)

    @staticmethod
    def _priority_indices(
        retention: Sequence[MessageRetentionDecision],
        priority: RetentionPriority,
    ) -> list[int]:
        return [idx for idx, decision in enumerate(retention) if decision.priority == priority]

    @staticmethod
    def _legal_suffix(messages: Sequence[dict[str, Any]]) -> bool:
        if not messages:
            return False
        if messages[0].get("role") == str(MessageRole.TOOL):
            return False
        declared: set[str] = set()
        for msg in messages:
            if msg.get("role") == str(MessageRole.ASSISTANT):
                for call in msg.get("tool_calls") or []:
                    if isinstance(call, dict) and call.get("id"):
                        declared.add(str(call["id"]))
            if msg.get("role") == str(MessageRole.TOOL):
                call_id = msg.get("tool_call_id")
                if call_id is not None and str(call_id) not in declared:
                    return False
        return True

    @staticmethod
    def _legal_sequence(messages: Sequence[dict[str, Any]]) -> bool:
        declared: set[str] = set()
        fulfilled: set[str] = set()
        for msg in messages:
            if msg.get("role") == str(MessageRole.ASSISTANT):
                for call in msg.get("tool_calls") or []:
                    if isinstance(call, dict) and call.get("id"):
                        declared.add(str(call["id"]))
            if msg.get("role") == str(MessageRole.TOOL):
                call_id = msg.get("tool_call_id")
                if call_id is None or str(call_id) not in declared:
                    return False
                fulfilled.add(str(call_id))
        return declared.issubset(fulfilled)

    @staticmethod
    def _fits(messages: Sequence[dict[str, Any]], budget: CompressionBudget) -> bool:
        if budget.max_keep_messages is not None and len(messages) > budget.max_keep_messages:
            return False
        return not (
            budget.max_keep_tokens is not None
            and estimate_token_count(messages) > budget.max_keep_tokens
        )
