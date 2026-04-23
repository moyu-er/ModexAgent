"""History archive search strategies.

Provides pluggable strategies for retrieving relevant history entries
from the archive layer. The default KeywordHistorySearch uses simple
token matching with stop-word filtering.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import Any


class HistorySearchStrategy(ABC):
    """归档检索策略抽象基类。

    实现类接收全部历史条目列表，根据 query 返回最相关的条目。
    所有计算在内存中进行，无外部依赖。
    """

    @abstractmethod
    async def search(
        self,
        entries: list[dict[str, Any]],
        query: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        """从历史条目列表中检索与 query 最相关的条目。

        Args:
            entries: 全部历史条目列表（通常来自 storage.read_logs）
            query: 用户查询字符串，用于提取关键词匹配
            limit: 最多返回的条目数

        Returns:
            按相关性排序的条目列表，长度不超过 limit
        """
        ...


class KeywordHistorySearch(HistorySearchStrategy):
    """基于关键词的简单检索策略。

    算法：
    1. 从 query 中提取关键词（去掉停用词）
    2. 计算每条 entry 的匹配分数 = 匹配关键词数 / 总关键词数
    3. 返回分数最高的 limit 条（仅返回分数 > 0 的条目）

    无外部依赖，纯内存计算，适合作为默认实现。
    """

    _STOP_WORDS: set[str] = {
        # 中文停用词
        "的", "是", "了", "在", "和", "有", "我", "你", "他", "她", "它",
        "我们", "你们", "他们", "这", "那", "个", "一", "不", "也", "就",
        "都", "而", "及", "与", "或", "但是", "然而", "因为", "所以",
        # 英文停用词
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
        """从文本中提取关键词（去掉停用词和单字符）。"""
        # 匹配中文单字、英文单词、数字
        words = re.findall(r"[\u4e00-\u9fa5]|[a-zA-Z0-9]+", text.lower())
        return {
            w for w in words
            if w not in self._STOP_WORDS and len(w) > 1
        }

    def _score(self, entry: dict[str, Any], keywords: set[str]) -> float:
        """计算单条 entry 与关键词的匹配分数。"""
        summary = entry.get("summary", "")
        if not summary or not keywords:
            return 0.0
        summary_lower = summary.lower()
        matched = sum(1 for kw in keywords if kw in summary_lower)
        return matched / len(keywords)

    async def search(
        self,
        entries: list[dict[str, Any]],
        query: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        keywords = self._extract_keywords(query)
        if not keywords:
            # 无有效关键词时回退到最近条目
            return entries[-limit:] if limit else entries

        scored = [(self._score(e, keywords), e) for e in entries]
        scored.sort(key=lambda x: x[0], reverse=True)
        return [e for s, e in scored[:limit] if s > 0]
