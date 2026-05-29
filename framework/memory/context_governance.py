"""Context governance — in-turn token budget management for LLM context.

Governance chain:
  lossy_compaction → tool_chain_repair → final_legality

All governance operates on a *copy* of messages; the persisted history
is never modified.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from enum import StrEnum
from typing import Any

from framework.core.types import MessageRole
from framework.memory.sanitizer import (
    DefaultSessionToolChainSanitizer,
    ToolChainSanitizationMode,
)
from framework.memory.core.scope import MemoryContext
from framework.memory.utils import estimate_token_count
from framework.memory.xml_truncate import truncate_xml_safe

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
    """Repair tool-call chain integrity by removing structurally invalid records.

    Uses the session tool-chain sanitizer in MODEL_VISIBLE_CONTEXT mode to
    remove orphan tool results and incomplete assistant/tool groups from the
    model-visible message copy. Incomplete tool-call groups are deleted rather
    than backfilled; LLM providers cannot receive assistant messages with
    tool_calls but no matching tool results.

    This pass also handles orphan tool results with no preceding assistant
    declaration.

    Operates on a message copy only — persisted history is never modified.
    """

    async def apply(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result = DefaultSessionToolChainSanitizer().sanitize(
            messages,
            mode=ToolChainSanitizationMode.MODEL_VISIBLE_CONTEXT,
        )
        return result.messages

class LossyContentCompactionGovernance(ContextGovernance):
    """Apply deterministic lossy reductions to LLM context copies only.

    Truncates oversized ``content`` fields by role and, for assistant
    messages with tool_calls, truncates oversized ``function.arguments``
    strings so that huge tool-call payloads (e.g. 71 KB write_file
    content) do not blow through the token budget.
    """

    def __init__(
        self,
        tool_result_head_chars: int = 1200,
        assistant_head_chars: int = 1200,
        agent_head_chars: int = -1,
        user_head_chars: int = -1,
        tool_args_head_chars: int = 2048,
        keep_range_count: int = 20,
        keep_range_ratio: float = 0.5,
    ) -> None:
        self._limits = {
            str(MessageRole.TOOL): tool_result_head_chars if isinstance(tool_result_head_chars, int) else None,
            str(MessageRole.ASSISTANT): assistant_head_chars if isinstance(assistant_head_chars, int) else None,
            str(MessageRole.AGENT): agent_head_chars if isinstance(agent_head_chars, int) else None,
            str(MessageRole.USER): user_head_chars if isinstance(user_head_chars, int) else None,
        }
        self._tool_args_head_chars = tool_args_head_chars
        self.keep_range_count = keep_range_count
        self.keep_range_ratio = max(0.0, min(1.0, keep_range_ratio))

    async def apply(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        length = len(messages)
        max_range = max(0, min(length - self.keep_range_count, int(length * (1.0 - self.keep_range_ratio))))
        if max_range <= 0:
            return messages
        for i, msg in enumerate(messages):
            updated = dict(msg)
            role = str(updated.get("role", ""))

            # system messages: never truncated
            if role == "system":
                result.append(updated)
                continue

            if i >= max_range:
                result.append(updated)
                continue

            limit = self._limits.get(role)
            content = updated.get("content")
            if limit is not None and limit > 0 and isinstance(content, str) and len(content) > limit:
                fmt = str(updated.get("content_format", "plain"))
                if fmt == "xml":
                    paths: list[str] = updated.get("truncatable_paths") or ["content"]
                    updated["content"] = truncate_xml_safe(content, limit, paths)
                else:
                    updated["content"] = self._truncate_content(
                        content, limit, role,
                        source_agent=str(updated.get("source_agent", "")),
                    )
                updated[META_CONTEXT_LOSSY] = True
                updated[META_ORIGINAL_CHARS] = len(content)
                updated[META_CONTEXT_REDUCTION] = self._reduction_name(role)
            # Truncate oversized tool_calls arguments
            if self._tool_args_head_chars > 0:
                updated = self._truncate_tool_args(updated)
            result.append(updated)
        return result

    def _truncate_tool_args(self, msg: dict[str, Any]) -> dict[str, Any]:
        """Truncate oversized tool call arguments with JSON-aware replacement.

        Long string values are shortened to a head prefix.  Instead of
        embedding a truncation note inside the value (which would produce
        invalid JSON), this method adds ``_gv_truncated`` and
        ``_gv_truncation_info`` metadata fields to the arguments object
        so the whole payload stays valid JSON.

        If the arguments string is not valid JSON it is left untouched.
        """
        import json

        tool_calls = msg.get("tool_calls")
        if not isinstance(tool_calls, list):
            return msg
        truncated = False
        new_tool_calls: list[dict[str, Any]] = []
        for tc in tool_calls:
            if not isinstance(tc, dict):
                new_tool_calls.append(tc)
                continue
            fn = tc.get("function")
            if not isinstance(fn, dict):
                new_tool_calls.append(tc)
                continue
            args = fn.get("arguments")
            if not isinstance(args, str) or len(args) <= self._tool_args_head_chars:
                new_tool_calls.append(tc)
                continue

            try:
                obj = json.loads(args)
            except json.JSONDecodeError:
                new_tool_calls.append(tc)
                continue

            if not isinstance(obj, dict):
                new_tool_calls.append(tc)
                continue

            replaced = self._replace_long_values(obj, len(args), self._tool_args_head_chars)
            if replaced is None:
                new_tool_calls.append(tc)
                continue

            truncated = True
            new_fn = dict(fn)
            new_fn["arguments"] = json.dumps(replaced, ensure_ascii=False)
            new_tc = dict(tc)
            new_tc["function"] = new_fn
            new_tool_calls.append(new_tc)
        if truncated:
            msg = dict(msg)
            msg["tool_calls"] = new_tool_calls
            msg[META_CONTEXT_LOSSY] = True
            msg[META_CONTEXT_REDUCTION] = ContextReductionType.CONTENT_TRUNCATED
        return msg

    @staticmethod
    def _replace_long_values(
        obj: dict[str, Any],
        original_chars: int,
        max_chars: int,
    ) -> dict[str, Any] | None:
        """Replace the longest string value in *obj* with a shortened
        head copy and add ``_gv_truncated`` / ``_gv_truncation_info``
        metadata fields.

        Returns a new dict, or None when no string value needs truncation.
        """
        import json
        longest_key: str | None = None
        longest_val: str = ""
        longest_len = 0
        for k, v in obj.items():
            if isinstance(v, str) and len(v) > longest_len:
                longest_key = k
                longest_val = v
                longest_len = len(v)

        if longest_key is None or longest_len == 0:
            return None

        excess = original_chars - max_chars
        if excess <= 0:
            return None

        # Compute how much of the longest value to keep.
        # The replacement dict adds _gv_truncated + _gv_truncation_info
        # fields which consume some of the saved budget.
        info = (
            f"Field '{longest_key}' truncated: "
            f"{longest_len:,} → ~{max(0, longest_len - excess):,} chars"
        )
        metadata_overhead = len(
            json.dumps({"_gv_truncated": True, "_gv_truncation_info": info},
                       ensure_ascii=False)
        ) + 40  # safety margin for JSON escaping

        new_val_len = longest_len - excess - metadata_overhead
        new_val_len = max(200, min(new_val_len, longest_len))

        result = dict(obj)
        result[longest_key] = longest_val[:new_val_len]
        result["_gv_truncated"] = True
        result["_gv_truncation_info"] = info
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
    """Final provider-legality pass for model-visible context.

    ToolChainRepairGovernance already removes all incomplete tool-call groups
    and orphan tool results upstream via the tool-chain sanitizer. This pass
    returns the messages unchanged; it exists for config compatibility so
    existing governance chains do not break.
    """

    async def apply(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return messages


class UserRetentionBufferInjectionGovernance(ContextGovernance):
    """Inject pruned conversation context from UserRetentionBuffer as system messages."""

    def __init__(
        self,
        urb,  # UserRetentionBuffer
        context_factory: Callable[[], MemoryContext] | None = None,
    ) -> None:
        self._urb = urb
        self._context_factory = context_factory

    async def apply(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        context = self._context_factory() if self._context_factory else None
        if context is None:
            return messages
        try:
            entries = await self._urb.get_entries(context)
        except Exception:
            return messages
        if not entries:
            return messages

        # Build XML
        import xml.sax.saxutils as saxutils
        lines = ['<pruned_conversation_context>']
        for e in entries:
            role_attr = ' role="agent"' if e.pruned_user_role == "agent" else ""
            lines.append(f'  <entry{role_attr}>')
            lines.append(f'    <pruned_user_content>{saxutils.escape(e.pruned_user_content)}</pruned_user_content>')
            if e.completing_assistant_content:
                lines.append(f'    <completing_assistant_content>{saxutils.escape(e.completing_assistant_content)}</completing_assistant_content>')
            lines.append('  </entry>')
        lines.append('</pruned_conversation_context>')

        pending_msg = {
            "role": "system",
            "content": "\n".join(lines),
            "content_format": "xml",
            "truncatable_paths": ["pruned_user_content", "completing_assistant_content"],
            "metadata": {"memory_source": "user_retention_buffer"},
        }
        insert_at = self._after_system_messages(messages)
        return [*messages[:insert_at], pending_msg, *messages[insert_at:]]

    @staticmethod
    def _after_system_messages(messages):
        index = 0
        while index < len(messages) and messages[index].get("role") == "system":
            index += 1
        return index


def _compact_xml_content(content: str, paths: list[str]) -> str:
    """Replace text inside truncatable_paths elements with compaction notice.

    Uses xml.etree.ElementTree for correct nested-element handling.
    Falls back to plain text marker on parse failure.
    """
    from xml.etree import ElementTree as ET

    try:
        root = ET.fromstring(content)
        for path in paths:
            for elem in root.iter(path):
                if elem.text and len(elem.text) > 0:
                    elem.text = f"[content compacted: {len(elem.text)} chars]"
        return ET.tostring(root, encoding="unicode")
    except ET.ParseError:
        return f"[XML content omitted: {len(content)} chars]"


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
            fmt = str(msg.get("content_format", "plain"))
            if fmt == "xml":
                paths: list[str] = msg.get("truncatable_paths") or ["content"]
                if updated is None:
                    updated = [dict(m) for m in messages]
                updated[idx]["content"] = _compact_xml_content(content, paths)
            else:
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
