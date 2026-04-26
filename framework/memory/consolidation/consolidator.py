"""Online Consolidator — token-budget triggered summarization with runtime prefix stripping."""

import logging
from collections.abc import Sequence
from typing import Any

from framework.core.provider import LLMProvider
from framework.core.types import LLMResponse
from framework.memory.compression.policies import SummaryStrategy
from framework.memory.compression.semantic_filter import SemanticMessageFilter
from framework.memory.compression.strategy import MessageFilterStrategy
from framework.memory.compression.tool_chain import _find_safe_truncation_count
from framework.memory.core.models import CompressionReason
from framework.memory.core.scope import MemoryContext
from framework.memory.utils import strip_runtime_prefixes

logger = logging.getLogger(__name__)


class Consolidator(SummaryStrategy):
    """在线 Consolidator：将过期的短期记忆消息压缩为摘要。

    特点：
    - 在调用 LLM 之前自动剥离 [Runtime Context] 前缀
    - 可作为 MemoryCompressionCoordinator 的 summary strategy 使用
    - LLM 失败时回退到简单摘要（不阻塞流程）
    """

    DEFAULT_SYSTEM_PROMPT = """You are a conversation summarizer.

Task: Summarize the provided conversation messages into a concise paragraph.

Rules:
1. Keep key information: user's main questions, assistant's core answers, important context
2. Be concise: remove redundant pleasantries and repetition
3. Max 200 characters
4. Output plain text only, no markdown, no JSON

If the conversation is empty or trivial, output: (no summary)"""

    def __init__(
        self,
        llm_provider: LLMProvider | None = None,
        max_summary_length: int = 200,
        system_prompt: str | None = None,
        filter_strategy: MessageFilterStrategy | None = None,
    ):
        self.llm = llm_provider
        self.max_summary_length = max_summary_length
        self.system_prompt = system_prompt or self.DEFAULT_SYSTEM_PROMPT
        self.filter_strategy = filter_strategy or SemanticMessageFilter()

    @staticmethod
    def strip_runtime_prefixes(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """剥离消息内容中的 [Runtime Context] 前缀（委托给共享工具函数）。"""
        return strip_runtime_prefixes(messages)

    async def summarize(
        self,
        messages: Sequence[dict[str, Any]],
        context: MemoryContext,
        reason: CompressionReason,
    ) -> str:
        """生成对话摘要。

        只压缩较旧的前半部分消息，保留最近的消息不动。
        如果没有配置 LLM provider，则回退到基于消息角色的简单摘要。
        """
        _ = context, reason
        dict_messages = list(messages)
        if not dict_messages:
            return ""

        # 保留最近的一半消息（至少保留 1 条），压缩较旧的部分
        # 使用 tool-chain 感知的安全分割，避免在 tool_call/tool_result 链中间切断
        split_point = max(1, len(dict_messages) // 2)
        safe_split = _find_safe_truncation_count(dict_messages, split_point)
        to_compress = dict_messages[:safe_split]

        cleaned = self.strip_runtime_prefixes(to_compress)

        sanitized = cleaned
        if self.filter_strategy is not None:
            sanitized = self.filter_strategy.sanitize(cleaned)

        if not sanitized:
            return ""

        if self.llm is not None:
            try:
                summary = await self._summarize_with_llm(sanitized)
                return f"[Consolidator] {summary}"
            except Exception as e:
                logger.warning("Consolidator LLM summarization failed: %s", e)

        # Fallback: simple heuristic summary
        summary = self._fallback_summary(sanitized)
        return f"[Consolidator] {summary}"

    async def _summarize_with_llm(self, messages: list[dict[str, Any]]) -> str:
        """调用 LLM 生成摘要。"""
        assert self.llm is not None
        formatted = self._format_messages(messages)
        chat_fn = getattr(self.llm, "chat_with_retry", self.llm.chat)
        response = await chat_fn(
            messages=[
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": f"Summarize:\n\n{formatted}"},
            ],
            temperature=0.3,
            max_tokens=300,
        )
        if isinstance(response, LLMResponse):
            summary = response.content or ""
        else:
            summary = response.strip() if isinstance(response, str) else str(response).strip()
        if len(summary) > self.max_summary_length:
            summary = summary[: self.max_summary_length - 3] + "..."
        return summary

    @staticmethod
    def _format_messages(messages: list[dict[str, Any]]) -> str:
        """格式化消息列表。"""
        lines = []
        for msg in messages:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            if not content:
                continue
            lines.append(f"[{role}] {content}")
        return "\n".join(lines)

    @staticmethod
    def _fallback_summary(messages: list[dict[str, Any]]) -> str:
        """LLM 失败时的回退摘要。"""
        roles = [m.get("role") for m in messages if m.get("content")]
        user_msgs = [m for m in messages if m.get("role") == "user" and m.get("content")]
        if user_msgs:
            topics = []
            for msg in user_msgs[-3:]:
                content = msg.get("content", "")
                topic = content[:40] + "..." if len(content) > 40 else content
                topics.append(topic)
            return f"对话涉及: {', '.join(topics)}"
        return f"{len(messages)} messages ({', '.join({str(r) for r in roles if r is not None})})"
