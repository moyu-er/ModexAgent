"""Knowledge retrieval strategies.

Provides pluggable strategies for retrieving knowledge files (SOUL, USER, MEMORY)
with query-aware filtering and token budget enforcement.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from modex_agent.memory.core.models import LongTermMemory
from modex_agent.memory.utils import estimate_text_tokens
from modex_agent.utils.helpers import strip_think

__all__ = [
    "FullDumpKnowledgeStrategy",
    "KnowledgeSearchStrategy",
]


class KnowledgeSearchStrategy(ABC):
    """Strategy for retrieving knowledge content within a token budget.

    Receives the full LongTermMemory and returns a (possibly filtered/truncated)
    version suitable for injection into the system prompt.
    """

    @abstractmethod
    async def retrieve(
        self,
        knowledge: LongTermMemory,
        query: str = "",
        max_tokens: int = 2000,
    ) -> LongTermMemory:
        """Return knowledge content filtered to *max_tokens*.

        Args:
            knowledge: Full knowledge from storage.
            query: Optional query for relevance filtering.
            max_tokens: Hard token budget for the returned content.

        Returns:
            A LongTermMemory with content trimmed to fit the budget.
        """
        ...


class FullDumpKnowledgeStrategy(KnowledgeSearchStrategy):
    """Default strategy: return all knowledge files, truncated to token budget.

    SOUL and USER are always included in full (they are small and essential).
    MEMORY.md is truncated first if the total exceeds *max_tokens*.
    All content is stripped of think tags.
    """

    async def retrieve(
        self,
        knowledge: LongTermMemory,
        query: str = "",
        max_tokens: int = 2000,
    ) -> LongTermMemory:
        _ = query

        soul = self._clean(knowledge.soul)
        user = self._clean(knowledge.user)
        memory = self._clean(knowledge.memory)
        custom = {k: self._clean(v) for k, v in knowledge.custom.items()}

        # SOUL + USER are always included
        base_tokens = estimate_text_tokens(soul) + estimate_text_tokens(user)
        remaining = max(0, max_tokens - base_tokens)

        # Custom files take from remaining budget (proportional)
        custom_tokens = sum(estimate_text_tokens(v) for v in custom.values())
        memory_budget = remaining

        if custom_tokens > 0:
            # Split remaining budget between custom and memory
            custom_budget = min(custom_tokens, remaining // 2)
            custom = self._truncate_dict(custom, custom_budget)
            memory_budget = remaining - sum(estimate_text_tokens(v) for v in custom.values())

        # MEMORY.md gets whatever is left
        memory = self._truncate_text(memory, memory_budget)

        return LongTermMemory(soul=soul, user=user, memory=memory, custom=custom)

    @staticmethod
    def _clean(text: str) -> str:
        """Strip think tags and trim whitespace."""
        result = strip_think(text)
        return result or ""

    @staticmethod
    def _truncate_text(text: str, max_tokens: int) -> str:
        """Truncate text to fit within token budget, respecting paragraph boundaries."""
        if not text:
            return ""
        if estimate_text_tokens(text) <= max_tokens:
            return text

        # Rough char-to-token ratio (1 token ≈ 1.5 chars for mixed content)
        max_chars = int(max_tokens * 1.5)
        if len(text) <= max_chars:
            return text

        # Truncate at paragraph boundary
        truncated = text[:max_chars]
        last_para = truncated.rfind("\n\n")
        if last_para > max_chars // 2:
            truncated = truncated[:last_para]
        return truncated

    def _truncate_dict(self, files: dict[str, str], max_tokens: int) -> dict[str, str]:
        """Truncate a dict of file contents to fit within token budget."""
        total = sum(estimate_text_tokens(v) for v in files.values())
        if total <= max_tokens:
            return files

        result: dict[str, str] = {}
        remaining = max_tokens
        for key, value in files.items():
            budget = min(remaining, estimate_text_tokens(value))
            result[key] = self._truncate_text(value, budget)
            remaining -= estimate_text_tokens(result[key])
            if remaining <= 0:
                break
        return result
