"""History archive search strategies."""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from typing import Any


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


class KeywordHistorySearch(HistorySearchStrategy):
    """Simple keyword search over archive summary and provenance fields."""

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

    def _extract_keywords(self, text: str) -> set[str]:
        lowered = text.lower()
        keywords: set[str] = {
            word
            for word in re.findall(r"[a-zA-Z0-9]+", lowered)
            if word not in self._STOP_WORDS and len(word) > 1
        }
        for segment in re.findall(r"[\u4e00-\u9fa5]+", lowered):
            cleaned = "".join(ch for ch in segment if ch not in self._STOP_WORDS)
            for size in range(2, min(4, len(cleaned)) + 1):
                keywords.update(
                    cleaned[index : index + size]
                    for index in range(0, len(cleaned) - size + 1)
                )
            if len(cleaned) == 1 and cleaned not in self._STOP_WORDS:
                keywords.add(cleaned)
        return keywords

    def _entry_text(self, entry: dict[str, Any]) -> str:
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

    def _score(self, entry: dict[str, Any], keywords: set[str]) -> float:
        if not keywords:
            return 0.0
        entry_text = self._entry_text(entry).lower()
        if not entry_text:
            return 0.0
        matched = sum(1 for keyword in keywords if keyword in entry_text)
        return matched / len(keywords)

    async def search(
        self,
        entries: list[dict[str, Any]],
        query: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        keywords = self._extract_keywords(query)
        if not keywords:
            return entries[-limit:] if limit else entries

        scored = [(self._score(entry, keywords), entry) for entry in entries]
        scored.sort(key=lambda item: item[0], reverse=True)
        return [entry for score, entry in scored[:limit] if score > 0]
