"""ContextGovernance implementations for pre-LLM context trimming and injection.

Governance runs on a COPY of the model-visible message list; persisted history
is never modified.
"""

from __future__ import annotations

import logging
from abc import abstractmethod
from collections.abc import Callable
from enum import StrEnum
from typing import Any

from modex_agent.core.types import MessageRole
from modex_agent.core.governance import ContextGovernance
from modex_agent.core.message import ContentFormat
from modex_agent.core.scope import MemoryContext
from modex_agent.memory.tags import UrbTag
from modex_agent.memory.token_estimator import CharTokenEstimator, TokenEstimator
from modex_agent.memory.xml_truncate import truncate_xml_safe

logger = logging.getLogger(__name__)


def estimate_token_count(messages: list[dict[str, Any]]) -> int:
    """Rough token estimate from character count (chars / 4)."""
    total = sum(len(str(m.get("content", ""))) for m in messages)
    return total // 4


META_CONTEXT_LOSSY = "meta_context_lossy"
META_ORIGINAL_CHARS = "meta_original_chars"
META_CONTEXT_REDUCTION = "meta_context_reduction"


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
    """Repair tool-call chain integrity in the model-visible message copy.

    Uses the session tool-chain sanitizer in MODEL_VISIBLE_CONTEXT mode to:
    - remove orphan tool results (no matching assistant tool_call), and
    - backfill dangling tool_calls: an assistant tool_call with no matching
      tool result is kept and a placeholder tool message (matched id) is
      synthesized so LLM providers never receive assistant messages with
      tool_calls but no matching tool results.

    Operates on a message copy only — persisted history is never modified.
    """

    async def apply(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        from modex_agent.memory.sanitizer import (
            DefaultSessionToolChainSanitizer,
            ToolChainSanitizationMode,
        )

        result = DefaultSessionToolChainSanitizer().sanitize(
            messages,
            mode=ToolChainSanitizationMode.MODEL_VISIBLE_CONTEXT,
        )
        return result.messages


_COMPACT_BUFFER = 20


class LossyContentCompactionGovernance(ContextGovernance):
    """Apply deterministic lossy reductions to LLM context copies only.

    Compaction happens in fixed-size steps.  When the conversation length
    exceeds ``n * compact_range_count + _COMPACT_BUFFER``, the oldest
    ``n * compact_range_count`` messages become candidates for compaction.
    Within a step the set of compacted messages does not change, so the
    prefix stabilizes and prompt caches can warm up.
    """

    def __init__(
        self,
        tool_result_head_chars: int = 1200,
        assistant_head_chars: int = 1200,
        agent_head_chars: int = 1200,
        user_head_chars: int = 1200,
        tool_args_head_chars: int = 2048,
        compact_range_count: int = 50,
        compact_buffer: int = _COMPACT_BUFFER,
    ) -> None:
        self._limits = {
            str(MessageRole.TOOL): tool_result_head_chars
            if isinstance(tool_result_head_chars, int)
            else None,
            str(MessageRole.ASSISTANT): assistant_head_chars
            if isinstance(assistant_head_chars, int)
            else None,
            str(MessageRole.AGENT): agent_head_chars if isinstance(agent_head_chars, int) else None,
            str(MessageRole.USER): user_head_chars if isinstance(user_head_chars, int) else None,
        }
        self._tool_args_head_chars = tool_args_head_chars
        self.compact_range_count = max(20, compact_range_count)
        self.compact_buffer = max(5, compact_buffer)

    async def apply(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        length = len(messages)
        buffer = self.compact_buffer
        if length <= buffer:
            return list(messages)

        # Step-based compaction: only touch whole blocks of compact_range_count
        # oldest messages.  The same block stays untouched until the next step.
        n = (length - buffer) // self.compact_range_count
        compact_count = n * self.compact_range_count
        if compact_count <= 0:
            return list(messages)

        result: list[dict[str, Any]] = []
        for i, msg in enumerate(messages):
            updated = dict(msg)
            role = str(updated.get("role", ""))

            # system messages: never truncated
            if role == "system":
                result.append(updated)
                continue

            # Only the oldest compact_count messages are candidates.
            if i >= compact_count:
                result.append(updated)
                continue

            limit = self._limits.get(role)
            content = updated.get("content")
            if (
                limit is not None
                and limit > 0
                and isinstance(content, str)
                and len(content) > limit
            ):
                fmt = str(updated.get("content_format", "plain"))
                if fmt == "xml":
                    paths: list[str] = updated.get("truncatable_paths") or ["content"]
                    updated["content"] = truncate_xml_safe(content, limit, paths)
                else:
                    updated["content"] = self._truncate_content(
                        content,
                        limit,
                        role,
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
        metadata_overhead = (
            len(
                json.dumps({"_gv_truncated": True, "_gv_truncation_info": info}, ensure_ascii=False)
            )
            + 40
        )  # safety margin for JSON escaping

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
        # Keep the suffix stable (no dynamic length) so identical content
        # compacted in different turns produces identical output and can
        # benefit from prompt caches.
        suffix = f"\n[Context content truncated for role={role}]"
        prefix = (
            f"[From Agent {source_agent}]\n"
            if role == str(MessageRole.AGENT) and source_agent
            else ""
        )
        body = content
        if prefix and body.startswith(prefix):
            body = body[len(prefix) :]
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


class UserRetentionBufferInjectionGovernance(ContextGovernance):
    """Inject recently pruned conversation fragments as a user message.

    The XML is inserted directly after the last system message (before
    conversation history). This is semantically correct — the entries
    represent recent dialogue between you and the user.
    """

    def __init__(
        self,
        urb,  # UserRetentionBuffer
        context_factory: Callable[[], MemoryContext] | None = None,
        max_entries: int = 5,
        max_user_content_chars: int = 800,
        max_assistant_content_chars: int = 400,
    ) -> None:
        self._urb = urb
        self._context_factory = context_factory
        self._max_entries = max_entries
        self._max_user_chars = max_user_content_chars
        self._max_assistant_chars = max_assistant_content_chars

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

        # Cap entries to max (default 5) — matches UserRetentionBufferConfig
        if len(entries) > self._max_entries:
            entries = entries[-self._max_entries :]

        from modex_agent.utils.xml import xml_text

        ct = UrbTag.CONTAINER.value
        et = UrbTag.ENTRY.value
        ut = UrbTag.USER_MSG.value
        yt = UrbTag.YOU_RESPONSE.value
        lines = [
            f"<{ct}>",
            "<!-- Pruned conversation history preserved from context cleanup. -->",
            "<!-- Each entry is one Q&A pair. entry without <you> = unanswered. -->",
            "<!-- Last 3 entries shown (FIFO). -->",
        ]
        for e in entries:
            user_text = self._truncate_entry_content(
                e.pruned_user_content,
                self._max_user_chars,
                e.content_format,
                e.truncatable_paths,
            )
            assistant_text = ""
            if e.completing_assistant_content:
                assistant_text = self._truncate_entry_content(
                    e.completing_assistant_content,
                    self._max_assistant_chars,
                    e.content_format,
                    e.truncatable_paths,
                )
            if not user_text and not assistant_text:
                continue

            role_attr = ' role="agent"' if e.pruned_user_role == str(MessageRole.AGENT) else ""
            lines.append(f"  <{et}{role_attr}>")
            lines.append(f"    <{ut}>{xml_text(user_text)}</{ut}>")
            if assistant_text:
                lines.append(f"    <{yt}>{xml_text(assistant_text)}</{yt}>")
            lines.append(f"  </{et}>")
        lines.append(f"</{ct}>")

        # Only emit the message if we have at least one non-empty entry
        if len(lines) <= 2:  # only opening + closing tags
            return messages

        urb_msg = {
            "role": str(MessageRole.USER),
            "content": "\n".join(lines),
            "content_format": ContentFormat.XML,
            "truncatable_paths": [UrbTag.USER_MSG.value, UrbTag.YOU_RESPONSE.value],
            "metadata": {"memory_source": "user_retention_buffer"},
        }
        insert_at = self._after_system_messages(messages)
        return [*messages[:insert_at], urb_msg, *messages[insert_at:]]

    @staticmethod
    def _truncate_entry_content(
        content: str,
        max_chars: int,
        content_format: str | None,
        truncatable_paths: list[str] | None,
    ) -> str:
        """Truncate entry content, preserving XML structure when applicable."""
        if not content or len(content) <= max_chars:
            return content
        if content_format == "xml":
            return truncate_xml_safe(content, max_chars, truncatable_paths or [])
        return content[:max_chars] + f"\n[...truncated, {len(content)} chars total]"

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


class ContextReductionType(StrEnum):
    """Standardized names for context-reduction metadata."""

    TOOL_RESULT_TRUNCATED = "tool_result_truncated"
    ASSISTANT_TRUNCATED = "assistant_truncated"
    AGENT_INPUT_TRUNCATED = "agent_input_truncated"
    USER_INPUT_TRUNCATED = "user_input_truncated"
    CONTENT_TRUNCATED = "content_truncated"


class TokenBudgetGovernance(ContextGovernance):
    """当消息列表超 token 预算时从开头截断，保留 system 和最近消息。"""

    def __init__(
        self,
        max_tokens: int,
        safety_buffer: int = 1024,
        token_estimator: TokenEstimator | None = None,
    ) -> None:
        self._max_tokens = max_tokens
        self._safety_buffer = safety_buffer
        self._estimator: TokenEstimator = token_estimator or CharTokenEstimator()

    async def apply(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not messages:
            return []

        system_messages = [
            dict(msg) for msg in messages if msg.get("role") == str(MessageRole.SYSTEM)
        ][:1]
        non_system = [dict(msg) for msg in messages if msg.get("role") != str(MessageRole.SYSTEM)]

        if not non_system:
            return system_messages

        system_tokens = self._estimator.estimate_messages(system_messages)
        remaining_budget = max(128, self._max_tokens - system_tokens - self._safety_buffer)

        # 从尾部向前累加，直到预算耗尽
        kept: list[dict[str, Any]] = []
        kept_tokens = 0
        for msg in reversed(non_system):
            msg_tokens = self._estimator.estimate_message(msg)
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
        self._whitelist_tools = (
            frozenset(whitelist_tools) if whitelist_tools is not None else frozenset()
        )

    async def apply(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        compactable_indices: list[int] = []
        for idx, msg in enumerate(messages):
            if (
                msg.get("role") == str(MessageRole.TOOL)
                and msg.get("name") not in self._whitelist_tools
            ):
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
