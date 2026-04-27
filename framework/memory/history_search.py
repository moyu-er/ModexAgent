"""History archive search strategies.

Provides pluggable strategies for retrieving relevant archive entries.
The default strategy returns recent entries — simple, reliable, no
external dependencies.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

__all__ = [
    "HistorySearchStrategy",
    "KeywordHistorySearch",
    "RecentFirstHistorySearch",
]


class HistorySearchStrategy(ABC):
    """Strategy for finding relevant archive entries."""

    @abstractmethod
    async def search(
        self,
        entries: list[dict[str, Any]],
        query: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Return archive entries that match ``query``."""
        ...


class RecentFirstHistorySearch(HistorySearchStrategy):
    """Default strategy: return the most recent entries.

    Reliable and predictable. No keyword matching gimmicks.
    If a query is provided, entries containing query terms are boosted
    to the top, but recency is the primary signal.
    """

    async def search(
        self,
        entries: list[dict[str, Any]],
        query: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        if not entries:
            return []

        recent = entries[-limit:] if limit else entries

        if not query or not query.strip():
            return recent

        # Simple relevance boost: move entries containing query terms forward
        query_lower = query.lower()
        terms = [t for t in query_lower.split() if len(t) > 1]
        if not terms:
            return recent

        def _relevance(entry: dict[str, Any]) -> int:
            text = (
                str(entry.get("summary", ""))
                + " "
                + str(entry.get("content", ""))
            ).lower()
            return sum(1 for t in terms if t in text)

        # Stable sort: relevance desc, then position desc (more recent first)
        indexed = list(enumerate(recent))
        indexed.sort(
            key=lambda pair: (_relevance(pair[1]), pair[0]),
            reverse=True,
        )
        return [entry for _, entry in indexed[:limit]]


class KeywordHistorySearch(HistorySearchStrategy):
    """Keyword-based search with term frequency scoring.

    Retained for backward compatibility but not recommended as default.
    """

    _STOP_WORDS: set[str] = {
        "the", "a", "an", "is", "are", "was", "were", "be", "been",
        "being", "have", "has", "had", "do", "does", "did", "will",
        "would", "could", "should", "may", "might", "must", "shall",
        "can", "need", "dare", "ought", "used", "to", "of", "in",
        "for", "on", "with", "at", "by", "from", "as", "into",
        "through", "during", "before", "after", "above", "below",
        "between", "under", "and", "but", "or", "yet", "so",
        "if", "because", "although", "though", "while", "where",
        "when", "that", "which", "who", "whom", "whose", "what",
        "this", "these", "those", "i", "you", "he", "she", "it",
        "we", "they", "me", "him", "her", "us", "them",
    }

    async def search(
        self,
        entries: list[dict[str, Any]],
        query: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        if not entries:
            return []
        import json
        import re

        query_lower = query.lower()

        # Extract Latin/number keywords
        keywords: set[str] = {
            word
            for word in re.findall(r"[a-zA-Z0-9]+", query_lower)
            if word not in self._STOP_WORDS and len(word) > 1
        }

        # Extract CJK segments as additional keywords
        for segment in re.findall(r"[\u4e00-\u9fa5]+", query_lower):
            if len(segment) >= 2:
                keywords.add(segment)

        if not keywords:
            return entries[-limit:] if limit else entries

        def _entry_text(entry: dict[str, Any]) -> str:
            parts: list[str] = []
            for key in ("summary", "content"):
                value = entry.get(key)
                if isinstance(value, str):
                    parts.append(value)
            for key in ("metadata", "raw_refs"):
                value = entry.get(key)
                if value:
                    parts.append(json.dumps(value, ensure_ascii=False, sort_keys=True))
            return "\n".join(parts)

        scored: list[tuple[float, dict[str, Any]]] = []
        for entry in entries:
            text = _entry_text(entry).lower()
            matched = sum(1 for kw in keywords if kw in text)
            scored.append((matched / len(keywords), entry))

        scored.sort(key=lambda item: item[0], reverse=True)
        return [entry for score, entry in scored[:limit] if score > 0]
