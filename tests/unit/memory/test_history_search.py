from __future__ import annotations

from framework.memory.history_search import KeywordHistorySearch


async def test_keyword_search_matches_chinese_query():
    search = KeywordHistorySearch()
    entries = [
        {"summary": "讨论 Python 数据分析项目"},
        {"summary": "用户询问天气和日程"},
    ]

    results = await search.search(entries, "数据分析", limit=1)

    assert results == [{"summary": "讨论 Python 数据分析项目"}]


async def test_keyword_search_matches_metadata_and_raw_refs():
    search = KeywordHistorySearch()
    entries = [
        {"summary": "天气闲聊", "metadata": {"topic": "weather"}},
        {"summary": "项目讨论", "metadata": {"tool": "notebook"}, "raw_refs": ["Python 数据分析"]},
    ]

    results = await search.search(entries, "数据分析", limit=1)

    assert results == [
        {"summary": "项目讨论", "metadata": {"tool": "notebook"}, "raw_refs": ["Python 数据分析"]}
    ]
