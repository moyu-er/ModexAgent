"""SummarizerStrategy — message summarization backed by SummarizerAgent.

Connects the SummarizerAgent to the archive generation pipeline,
with message preprocessing (prefix stripping + formatting).
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import Any

from modex_agent.agents.summarizer.agent import SummarizerAgent
from modex_agent.memory.core.models import CompressionReason
from modex_agent.memory.core.scope import MemoryContext
from modex_agent.memory.utils import (
    EMPTY_MEMORY_SUMMARY_MARKERS,
    _is_meaningless_summary,
    strip_runtime_prefixes,
)

logger = logging.getLogger(__name__)


class SummarizerStrategy(ABC):
    """Abstract base for summarization strategies used in archive generation.

    Replaces the former SummaryStrategy from the deleted compression module.
    """

    @abstractmethod
    async def summarize(
        self,
        messages: Sequence[dict[str, Any]],
        context: MemoryContext,
        reason: CompressionReason,
    ) -> str:
        """Summarize a sequence of messages."""
        ...

    def _format_messages(
        self,
        messages: list[dict[str, Any]],
        *,
        max_tool_result_chars: int = 200,
    ) -> str:
        """Format messages into a plain-text transcript for the LLM."""
        lines: list[str] = []
        for msg in messages:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            if not content and not msg.get("tool_calls"):
                continue

            if role == "assistant" and msg.get("tool_calls"):
                tool_names: list[str] = []
                for tc in msg.get("tool_calls", []):
                    fn = tc.get("function", {}) if isinstance(tc, dict) else {}
                    tool_names.append(fn.get("name", "?"))
                if content:
                    lines.append(f"[assistant -> tools: {', '.join(tool_names)}] {content}")
                else:
                    lines.append(f"[assistant -> tools: {', '.join(tool_names)}]")
                continue

            if role == "tool":
                name = msg.get("name", "unknown")
                if isinstance(content, str) and len(content) > max_tool_result_chars:
                    content = content[:max_tool_result_chars] + "..."
                    content += f" ({len(msg.get('content', ''))} chars total)"
                lines.append(f"[tool:{name}] {content}")
                continue

            if not content:
                continue
            lines.append(f"[{role}] {content}")
        return "\n".join(lines)

    @staticmethod
    def _fallback_summary(messages: list[dict[str, Any]]) -> str:
        """Heuristic fallback when the LLM summarizer fails."""
        user_msgs = [m for m in messages if m.get("role") == "user" and m.get("content")]
        if user_msgs:
            topics: list[str] = []
            for msg in user_msgs[-3:]:
                msg_content = msg.get("content", "")
                topic = msg_content[:60] + "..." if len(msg_content) > 60 else msg_content
                topics.append(topic)
            return f"[Consolidator] conversation topics: {', '.join(topics)}"
        assistant_msgs = [m for m in messages if m.get("role") == "assistant" and m.get("content")]
        if assistant_msgs:
            return f"[Consolidator] {len(messages)} messages (assistant replies: {len(assistant_msgs)})"
        return f"[Consolidator] {len(messages)} messages"


class DefaultSummarizerStrategy(SummarizerStrategy):
    """SummarizerStrategy that delegates to SummarizerAgent.

    Preprocesses messages before passing to the agent:
    1. Strips runtime context prefixes
    2. Formats messages into a plain-text transcript

    Falls back to heuristic summary if the agent fails.
    """

    def __init__(
        self,
        agent: SummarizerAgent,
        max_summary_length: int = 800,
    ) -> None:
        self.agent = agent
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

        if not cleaned:
            return ""

        # Format for the agent
        formatted = self._format_messages(cleaned)
        if not formatted.strip():
            return ""

        # Delegate to SummarizerAgent
        summary = await self.agent.summarize(
            formatted,
            prompt=SummarizerAgent.PROMPT_MEMORY_COMPRESSION,
            max_tokens=self.max_summary_length,
        )

        if not summary:
            return self._fallback_summary(cleaned)

        if len(summary) < 100 and summary.strip() in EMPTY_MEMORY_SUMMARY_MARKERS:
            return ""

        if _is_meaningless_summary(summary):
            return ""

        return summary
