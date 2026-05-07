"""Context governance — in-turn token budget management for LLM context.

Governance chain:
  drop_orphans → backfill → microcompact → tool_budget → snip_history

All governance operates on a *copy* of messages; the persisted history
is never modified.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import StrEnum
from typing import Any

from framework.core.types import MessageRole
from framework.memory.retention import DefaultMessageRetentionPolicy, MessageRetentionPolicy
from framework.memory.retention.types import MessageRetentionDecision, RetentionPriority
from framework.memory.utils import estimate_token_count

TOOL_RESULT_UNAVAILABLE_CONTENT = (
    "[Tool result unavailable - the tool call may have been interrupted "
    "or its result was removed from the model-visible context]"
)


class ContextReductionType(StrEnum):
    """Type of lossy reduction applied to a message for governance."""

    TOOL_RESULT_TRUNCATED = "tool_result_truncated"
    ASSISTANT_TRUNCATED = "assistant_truncated"
    AGENT_INPUT_TRUNCATED = "agent_input_truncated"
    USER_INPUT_TRUNCATED = "user_input_truncated"
    CONTENT_TRUNCATED = "content_truncated"


# Metadata keys used by governance to mark lossy-compacted messages
META_CONTEXT_LOSSY = "meta_context_lossy"
META_ORIGINAL_CHARS = "meta_original_chars"
META_CONTEXT_REDUCTION = "meta_context_reduction"


class ContextGovernance(ABC):
    """轮内上下文治理抽象基类。

    在每次 LLM 调用前对消息列表进行调整，确保不超出 token 预算
    或上下文窗口限制。所有实现必须返回新的消息列表副本，不得
    修改原始输入。
    """

    @abstractmethod
    async def apply(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """应用治理策略，返回调整后的消息列表副本。

        Args:
            messages: 原始消息列表（system + history + current turn）

        Returns:
            新的消息列表副本，可能经过截断、压缩或修复
        """
        ...


class CompositeGovernance(ContextGovernance):
    """按顺序组合多个治理策略。"""

    def __init__(self, strategies: list[ContextGovernance]) -> None:
        self._strategies = strategies

    async def apply(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result = list(messages)
        for strategy in self._strategies:
            result = await strategy.apply(result)
        return result


class ToolChainRepairGovernance(ContextGovernance):
    """修复 tool-call 链完整性，在每次 LLM 调用前对消息列表进行兜底修复。

    统一处理两种常见的消息序列断裂场景（可能由崩溃、checkpoint 恢复、
    异常中断等原因引起）：

    1. **移除孤儿 tool 结果（orphan drop）**：如果存在 tool 消息但
       之前没有对应的 assistant 消息声明该 tool_call_id，说明该 tool
       结果已失去上下文，直接移除，避免 LLM 见到"来源不明"的 tool 结果。

    2. **补全缺失的 tool 结果（backfill）**：如果 assistant 消息声明了
       tool_calls 但对应 tool_call_id 的 tool 消息缺失，在 assistant
       之后插入占位 tool 结果，避免 LLM 收到含 tool_calls 但无 tool
       响应的断裂消息序列。

    此策略在 ReActAgent.run() 每次迭代的 LLM 请求前通过
    context.governance.apply() 调用。操作在消息副本上进行，
    不修改持久化历史。

    Note: multi_agent/governance.py 曾包含与此功能重复的
    _drop_orphan_tool_results / _backfill_missing_tool_results，
    现已统一下沉到此实现。
    """

    _BACKFILL_CONTENT = (
        "[Tool result unavailable — the tool call may have been interrupted "
        "or its result was lost before the response could be recorded]"
    )
    _BACKFILL_CONTENT = TOOL_RESULT_UNAVAILABLE_CONTENT

    async def apply(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        # Step 1: drop orphans
        declared: set[str] = set()
        dropped: list[dict[str, Any]] | None = None
        for idx, msg in enumerate(messages):
            role = msg.get("role")
            if role == str(MessageRole.ASSISTANT):
                for tc in msg.get("tool_calls") or []:
                    if isinstance(tc, dict) and tc.get("id"):
                        declared.add(str(tc["id"]))
            if role == str(MessageRole.TOOL):
                tid = msg.get("tool_call_id")
                if tid and str(tid) not in declared:
                    if dropped is None:
                        dropped = [dict(m) for m in messages[:idx]]
                    continue
            if dropped is not None:
                dropped.append(dict(msg))

        cleaned = dropped if dropped is not None else list(messages)

        # Step 2: backfill missing
        declared_calls: list[tuple[int, str, str]] = []
        fulfilled: set[str] = set()
        for idx, msg in enumerate(cleaned):
            role = msg.get("role")
            if role == str(MessageRole.ASSISTANT):
                for tc in msg.get("tool_calls") or []:
                    if isinstance(tc, dict) and tc.get("id"):
                        name = ""
                        func = tc.get("function")
                        if isinstance(func, dict):
                            name = func.get("name", "")
                        declared_calls.append((idx, str(tc["id"]), name))
            elif role == str(MessageRole.TOOL):
                tid = msg.get("tool_call_id")
                if tid:
                    fulfilled.add(str(tid))

        missing = [(ai, cid, name) for ai, cid, name in declared_calls if cid not in fulfilled]
        if not missing:
            return cleaned

        updated = list(cleaned)
        for offset, (assistant_idx, call_id, name) in enumerate(missing):
            insert_at = assistant_idx + 1 + offset
            while insert_at < len(updated) and updated[insert_at].get("role") == str(MessageRole.TOOL):
                insert_at += 1
            updated.insert(
                insert_at,
                {
                    "role": str(MessageRole.TOOL),
                    "tool_call_id": call_id,
                    "name": name,
                    "content": self._BACKFILL_CONTENT,
                },
            )
        return updated

class PriorityBudgetGovernance(ContextGovernance):
    """Select model-visible messages using retention priorities."""

    def __init__(
        self,
        max_tokens: int,
        retention_policy: MessageRetentionPolicy | None = None,
        safety_buffer: int = 1024,
        min_recent_user_turns: int | None = None,
        min_recent_agent_turns: int | None = None,
    ) -> None:
        self._max_tokens = max_tokens
        self._safety_buffer = safety_buffer
        self._retention = retention_policy or DefaultMessageRetentionPolicy()
        self._min_recent_user_turns = (
            min_recent_user_turns
            if min_recent_user_turns is not None
            else int(getattr(self._retention, "min_recent_user_turns", 1))
        )
        self._min_recent_agent_turns = (
            min_recent_agent_turns
            if min_recent_agent_turns is not None
            else int(getattr(self._retention, "min_recent_agent_turns", 1))
        )

    async def apply(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not messages:
            return []
        budget = max(128, self._max_tokens - self._safety_buffer)
        selected: list[dict[str, Any]] = []
        selected_tokens = 0
        decisions = [
            self._retention.decide(msg, index=idx, messages=messages)
            for idx, msg in enumerate(messages)
        ]
        protected_indices = self._recent_anchor_indices(
            decisions,
            RetentionPriority.USER_INPUT,
            self._min_recent_user_turns,
        )
        protected_indices.update(
            self._recent_anchor_indices(
                decisions,
                RetentionPriority.AGENT_INPUT,
                self._min_recent_agent_turns,
            )
        )
        ranked_indices = sorted(range(len(messages)), key=lambda idx: (decisions[idx].rank, -idx))
        kept_indices: set[int] = set()
        for idx in sorted(protected_indices):
            msg = dict(messages[idx])
            kept_indices.add(idx)
            selected_tokens += estimate_token_count([msg])
        for idx in ranked_indices:
            if idx in kept_indices:
                continue
            msg = dict(messages[idx])
            token_count = estimate_token_count([msg])
            if kept_indices and selected_tokens + token_count > budget:
                continue
            kept_indices.add(idx)
            selected_tokens += token_count
        for idx, msg in enumerate(messages):
            if idx in kept_indices:
                selected.append(dict(msg))
        return selected

    @staticmethod
    def _recent_anchor_indices(
        decisions: list[MessageRetentionDecision],
        priority: RetentionPriority,
        limit: int,
    ) -> set[int]:
        if limit <= 0:
            return set()
        matches = [idx for idx, decision in enumerate(decisions) if decision.priority == priority]
        return set(matches[-limit:])


class LossyContentCompactionGovernance(ContextGovernance):
    """Apply deterministic lossy reductions to LLM context copies only."""

    def __init__(
        self,
        tool_result_head_chars: int = 1200,
        assistant_head_chars: int = 1200,
        agent_head_chars: int = 2000,
        user_head_chars: int = 4000,
    ) -> None:
        self._limits = {
            str(MessageRole.TOOL): tool_result_head_chars,
            str(MessageRole.ASSISTANT): assistant_head_chars,
            str(MessageRole.AGENT): agent_head_chars,
            str(MessageRole.USER): user_head_chars,
        }

    async def apply(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for msg in messages:
            updated = dict(msg)
            role = str(updated.get("role", ""))
            limit = self._limits.get(role)
            content = updated.get("content")
            if limit is not None and isinstance(content, str) and len(content) > limit:
                updated["content"] = self._truncate_content(
                    content,
                    limit,
                    role,
                    source_agent=str(updated.get("source_agent", "")),
                )
                updated[META_CONTEXT_LOSSY] = True
                updated[META_ORIGINAL_CHARS] = len(content)
                updated[META_CONTEXT_REDUCTION] = self._reduction_name(role)
            result.append(updated)
        return result

    @staticmethod
    def _truncate_content(
        content: str,
        limit: int,
        role: str,
        *,
        source_agent: str = "",
    ) -> str:
        suffix = f"\n[Context content truncated for role={role}; original chars={len(content)}]"
        prefix = f"[From Agent {source_agent}]\n" if role == str(MessageRole.AGENT) and source_agent else ""
        body = content
        if prefix and body.startswith(prefix):
            body = body[len(prefix):]
        reserved = len(prefix) + len(suffix)
        if prefix and reserved >= limit:
            return prefix + suffix.lstrip()
        if prefix:
            head_limit = max(0, limit - reserved)
            return prefix + body[:head_limit] + suffix
        head_limit = max(0, limit - len(suffix))
        return body[:head_limit] + suffix

    @staticmethod
    def _reduction_name(role: str) -> ContextReductionType:
        if role == str(MessageRole.TOOL):
            return ContextReductionType.TOOL_RESULT_TRUNCATED
        if role == str(MessageRole.ASSISTANT):
            return ContextReductionType.ASSISTANT_TRUNCATED
        if role == str(MessageRole.AGENT):
            return ContextReductionType.AGENT_INPUT_TRUNCATED
        if role == str(MessageRole.USER):
            return ContextReductionType.USER_INPUT_TRUNCATED
        return ContextReductionType.CONTENT_TRUNCATED


class FinalContextLegalityGovernance(ContextGovernance):
    """Final provider-legality pass for model-visible context."""

    async def apply(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        declared: set[str] = set()
        fulfilled: set[str] = set()
        result: list[dict[str, Any]] = []
        for msg in messages:
            role = msg.get("role")
            if role == str(MessageRole.ASSISTANT):
                for tc in msg.get("tool_calls") or []:
                    if isinstance(tc, dict) and tc.get("id"):
                        declared.add(str(tc["id"]))
                result.append(dict(msg))
                continue
            if role == str(MessageRole.TOOL):
                call_id = msg.get("tool_call_id")
                if call_id is not None and str(call_id) in declared:
                    fulfilled.add(str(call_id))
                    result.append(dict(msg))
                continue
            result.append(dict(msg))
        missing = declared - fulfilled
        if not missing:
            return result

        updated = list(result)
        for call_id in sorted(missing):
            insert_at = self._find_tool_insert_position(updated, call_id)
            updated.insert(
                insert_at,
                {
                    "role": str(MessageRole.TOOL),
                    "tool_call_id": call_id,
                    "content": TOOL_RESULT_UNAVAILABLE_CONTENT,
                    META_CONTEXT_LOSSY: True,
                    META_CONTEXT_REDUCTION: ContextReductionType.CONTENT_TRUNCATED,
                },
            )
        return updated

    @staticmethod
    def _find_tool_insert_position(messages: list[dict[str, Any]], call_id: str) -> int:
        for idx, msg in enumerate(messages):
            if msg.get("role") != str(MessageRole.ASSISTANT):
                continue
            for tc in msg.get("tool_calls") or []:
                if isinstance(tc, dict) and str(tc.get("id", "")) == call_id:
                    insert_at = idx + 1
                    while insert_at < len(messages) and messages[insert_at].get("role") == str(MessageRole.TOOL):
                        insert_at += 1
                    return insert_at
        return len(messages)


class MicrocompactGovernance(ContextGovernance):
    """将旧的可压缩 tool result 替换为一行摘要，保留最近 N 个。"""
    def __init__(
        self,
        keep_recent: int = 10,
        min_chars: int = 200,
        whitelist_tools: set[str] | None = None,
    ) -> None:
        self._keep_recent = keep_recent
        self._min_chars = min_chars
        self._whitelist_tools = frozenset(whitelist_tools) if whitelist_tools is not None else frozenset()

    async def apply(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        compactable_indices: list[int] = []
        for idx, msg in enumerate(messages):
            if msg.get("role") == str(MessageRole.TOOL) and msg.get("name") not in self._whitelist_tools:
                compactable_indices.append(idx)

        if len(compactable_indices) <= self._keep_recent:
            return list(messages)

        stale = compactable_indices[: len(compactable_indices) - self._keep_recent]
        updated: list[dict[str, Any]] | None = None
        for idx in stale:
            msg = messages[idx]
            content = msg.get("content")
            if not isinstance(content, str) or len(content) < self._min_chars:
                continue
            name = msg.get("name", "tool")
            summary = f"[{name} result omitted from context: {len(content):,} chars]"
            if updated is None:
                updated = [dict(m) for m in messages]
            updated[idx]["content"] = summary

        return updated if updated is not None else list(messages)


class TokenBudgetGovernance(ContextGovernance):
    """当消息列表超 token 预算时从开头截断，保留 system 和最近消息。"""

    def __init__(
        self,
        max_tokens: int,
        safety_buffer: int = 1024,
    ) -> None:
        self._max_tokens = max_tokens
        self._safety_buffer = safety_buffer

    async def apply(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not messages:
            return []

        system_messages = [dict(msg) for msg in messages if msg.get("role") == str(MessageRole.SYSTEM)][:1]
        non_system = [dict(msg) for msg in messages if msg.get("role") != str(MessageRole.SYSTEM)]

        if not non_system:
            return system_messages

        system_tokens = estimate_token_count(system_messages)
        remaining_budget = max(128, self._max_tokens - system_tokens - self._safety_buffer)

        # 从尾部向前累加，直到预算耗尽
        kept: list[dict[str, Any]] = []
        kept_tokens = 0
        for msg in reversed(non_system):
            msg_tokens = estimate_token_count([msg])
            if kept and kept_tokens + msg_tokens > remaining_budget:
                break
            kept.append(msg)
            kept_tokens += msg_tokens
        kept.reverse()

        # 确保保留的消息以 user 消息开头（满足多数 LLM API 的交替要求）
        if kept:
            for i, msg in enumerate(kept):
                if msg.get("role") == str(MessageRole.USER):
                    kept = kept[i:]
                    break
            else:
                # 找不到 user 消息，回退到保留最近一条 user
                for msg in reversed(non_system):
                    if msg.get("role") == str(MessageRole.USER):
                        kept = [dict(msg)]
                        break

        return system_messages + kept
