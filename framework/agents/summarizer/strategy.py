"""SummarizerStrategy — SummaryStrategy adapter backed by SummarizerAgent.

Connects the SummarizerAgent to the compression pipeline via the
SummaryStrategy interface, with message preprocessing.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from framework.agents.summarizer.agent import SummarizerAgent
from framework.memory.compression.policies import SummaryStrategy
from framework.memory.compression.semantic_filter import SemanticMessageFilter
from framework.memory.compression.strategy import MessageFilterStrategy
from framework.memory.core.models import CompressionReason
from framework.memory.core.scope import MemoryContext
from framework.memory.utils import strip_runtime_prefixes

logger = logging.getLogger(__name__)


class SummarizerStrategy(SummaryStrategy):
    """SummaryStrategy that delegates to SummarizerAgent.

    Preprocesses messages before passing to the agent:
    1. Strips runtime context prefixes
    2. Applies semantic message filtering (removes low-value tool results)

    Falls back to heuristic summary if the agent fails.
    """

    def __init__(
        self,
        agent: SummarizerAgent,
        filter_strategy: MessageFilterStrategy | None = None,
        max_summary_length: int = 300,
    ) -> None:
        self.agent = agent
        self.filter_strategy = filter_strategy or SemanticMessageFilter()
        self.max_summary_length = max_summary_length

    async def summarize(
        self,
        messages: Sequence[dict[str, Any]],
        context: MemoryContext,
        reason: CompressionReason,
    ) -> str:
        _ = context, reason
        dict_messages = list(messages)
        if not dict_messages:
            return ""

        # Preprocess: strip runtime prefixes
        cleaned = strip_runtime_prefixes(dict_messages)

        # Preprocess: semantic filtering
        sanitized = self.filter_strategy.sanitize(cleaned) if self.filter_strategy else cleaned
        if not sanitized:
            return ""

        # Format for the agent
        formatted = self._format_messages(sanitized)
        if not formatted.strip():
            return ""

        # Delegate to SummarizerAgent
        summary = await self.agent.summarize(
            formatted,
            prompt=SummarizerAgent.PROMPT_COMPRESSION,
            max_tokens=self.max_summary_length,
        )

        if not summary or summary.strip() in ("(no summary)", "(nothing)"):
            return self._fallback_summary(sanitized)

        return f"[Consolidator] {summary}"

    @staticmethod
    def _format_messages(messages: list[dict[str, Any]]) -> str:
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
        user_msgs = [m for m in messages if m.get("role") == "user" and m.get("content")]
        if user_msgs:
            topics = []
            for msg in user_msgs[-3:]:
                content = msg.get("content", "")
                topic = content[:60] + "..." if len(content) > 60 else content
                topics.append(topic)
            return f"[Consolidator] 对话涉及: {', '.join(topics)}"
        assistant_msgs = [m for m in messages if m.get("role") == "assistant" and m.get("content")]
        if assistant_msgs:
            return f"[Consolidator] {len(messages)} messages (assistant replies: {len(assistant_msgs)})"
        return f"[Consolidator] {len(messages)} messages"
