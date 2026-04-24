"""Archive strategies for history persistence.

Provides pluggable strategies for how pruned short-term memory messages
are archived into the history layer.
"""

import json
import re
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from framework.memory.compression.semantic_filter import SemanticMessageFilter
from framework.memory.compression.strategy import MessageFilterStrategy
from framework.memory.core.compression import CompressionResult
from framework.memory.core.message import ChatMessage
from framework.memory.core.scope import MemoryContext
from framework.memory.utils import strip_runtime_prefixes


def _raw_archive_summary(messages: Sequence[ChatMessage | dict[str, Any]]) -> str:
    """将消息序列化为可读的原始摘要（自动清洗 Runtime Context 前缀）。"""
    dict_messages = [
        m.to_dict() if isinstance(m, ChatMessage) else m for m in messages
    ]
    sanitized = strip_runtime_prefixes(dict_messages)
    parts: list[str] = []
    for msg in sanitized:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        if content:
            parts.append(f"{role}: {content}")
        elif msg.get("tool_calls"):
            parts.append(f"{role}: [tool_calls]")
        elif msg.get("tool_call_id"):
            parts.append(f"{role}: [result]")
    if parts:
        return "\n".join(parts)
    return json.dumps(sanitized, ensure_ascii=False)


@dataclass
class ArchiveEntry:
    """结构化归档条目。

    相比原始字符串摘要，ArchiveEntry 提供了摘要文本和可搜索的元数据，
    便于未来的 RAG、DreamEngine 等组件进行基于 key 的检索。
    """

    summary: str
    metadata: dict[str, Any] = field(default_factory=dict)


class ArchiveStrategy(ABC):
    """负责将被裁剪的短期记忆消息持久化为历史摘要。"""

    @abstractmethod
    async def archive(
        self,
        context: MemoryContext,
        pruned_messages: Sequence[ChatMessage | dict[str, Any]],
        compression_result: CompressionResult,
        history_manager: Any,
    ) -> None:
        """归档被裁剪的消息。

        Args:
            context: 记忆上下文
            pruned_messages: 被裁剪掉的消息列表
            compression_result: 压缩策略返回的结果（可能包含 summary）
            history_manager: HistoryArchiveManager 实例
        """
        pass


class PreserveSummaryArchiveStrategy(ArchiveStrategy):
    """优先使用压缩结果中的 summary；无 summary 时降级为 raw dump。"""

    async def archive(
        self,
        context: MemoryContext,
        pruned_messages: Sequence[ChatMessage | dict[str, Any]],
        compression_result: CompressionResult,
        history_manager: Any,
    ) -> None:
        summary = compression_result.summary
        if summary:
            await history_manager.append(
                context,
                summary,
                {"pruned_count": len(pruned_messages), "source": "compression_summary"},
            )
        else:
            raw = _raw_archive_summary(pruned_messages)
            await history_manager.append(
                context,
                raw,
                {"pruned_count": len(pruned_messages), "source": "raw_dump_fallback"},
            )


class RawDumpArchiveStrategy(ArchiveStrategy):
    """直接以 JSON 序列化格式 dump 原始消息，不经过 LLM 处理。"""

    async def archive(
        self,
        context: MemoryContext,
        pruned_messages: Sequence[ChatMessage | dict[str, Any]],
        compression_result: CompressionResult,
        history_manager: Any,
    ) -> None:
        _ = compression_result  # intentionally unused: raw dump ignores LLM summary
        raw = _raw_archive_summary(pruned_messages)
        await history_manager.append(
            context,
            raw,
            {"pruned_count": len(pruned_messages), "source": "raw_dump"},
        )


class SemanticArchiveStrategy(ArchiveStrategy):
    """语义感知的归档策略。

    - 优先使用 LLM 生成的 compression_summary；
    - 无 LLM 摘要时，对 pruned_messages 进行语义过滤，生成摘要；
    - 过滤后无保留内容时，存储占位条目；
    - 绝不将原始 tool dump 直接写入 summary。
    """

    def __init__(self, filter_strategy: MessageFilterStrategy | None = None) -> None:
        self.filter_strategy = filter_strategy or SemanticMessageFilter()

    async def archive(
        self,
        context: MemoryContext,
        pruned_messages: Sequence[ChatMessage | dict[str, Any]],
        compression_result: CompressionResult,
        history_manager: Any,
    ) -> None:
        entry = self._build_entry(pruned_messages, compression_result)
        await history_manager.append(
            context,
            entry.summary,
            {
                **entry.metadata,
                "pruned_count": len(pruned_messages),
            },
        )

    def _build_entry(
        self,
        pruned_messages: Sequence[ChatMessage | dict[str, Any]],
        compression_result: CompressionResult,
    ) -> ArchiveEntry:
        if compression_result.summary:
            return ArchiveEntry(
                summary=compression_result.summary,
                metadata={
                    "source": "compression_summary",
                    "semantic_count": len(pruned_messages),
                },
            )

        dict_messages = [
            m.to_dict() if isinstance(m, ChatMessage) else m for m in pruned_messages
        ]
        sanitized = self.filter_strategy.sanitize(dict_messages)
        if sanitized:
            summary = self._heuristic_summary(sanitized)
            return ArchiveEntry(
                summary=summary,
                metadata={
                    "source": "sanitized_fallback",
                    "semantic_count": len(sanitized),
                },
            )

        return ArchiveEntry(
            summary="(no semantic content)",
            metadata={
                "source": "empty",
                "semantic_count": 0,
            },
        )

    @staticmethod
    def _heuristic_summary(messages: list[dict[str, Any]]) -> str:
        """基于清洗后消息生成启发式摘要。

        规则：
        1. 提取每个 user 消息的第一句话；
        2. 连接这些句子，若总长度超过 60 字符则截断并加省略号；
        3. 若检测到工具调用痕迹，末尾追加工具名称提示。
        """
        user_sentences: list[str] = []
        tool_names: list[str] = []

        for msg in messages:
            role = msg.get("role")
            content = msg.get("content", "")
            if role == "user" and content:
                # 提取第一句话（按 . ? ! 分割）
                first_sentence = content
                for delim in (".", "？", "?", "！", "!"):
                    if delim in first_sentence:
                        first_sentence = first_sentence.split(delim)[0] + delim
                        break
                user_sentences.append(first_sentence.strip())
            elif role == "assistant" and content:
                # 检查折叠后的孤儿链提示
                if "[Called tools:" in content:
                    match = re.search(r"\[Called tools:([^\]]+)\]", content)
                    if match:
                        names = [n.strip() for n in match.group(1).split(",") if n.strip()]
                        tool_names.extend(names)

        summary = " ".join(user_sentences) if user_sentences else ""
        if not summary.strip():
            # 没有用户句子时，退化为描述性文本
            roles = [m.get("role") for m in messages if m.get("role")]
            summary = f"对话涉及: {', '.join({str(r) for r in roles})}"

        if len(summary) > 60:
            summary = summary[:60].rstrip() + "..."

        if tool_names and len(summary) < 50:
            hint = f" (tools: {', '.join(set(tool_names))})"
            if len(summary) + len(hint) <= 65:
                summary += hint

        return summary


__all__ = [
    "ArchiveEntry",
    "ArchiveStrategy",
    "PreserveSummaryArchiveStrategy",
    "RawDumpArchiveStrategy",
    "SemanticArchiveStrategy",
]
