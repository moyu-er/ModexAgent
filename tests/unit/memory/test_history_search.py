"""Tests for HistorySearchStrategy and KeywordHistorySearch."""

import pytest

from framework.memory.history_search import KeywordHistorySearch


@pytest.fixture
def sample_entries():
    return [
        {"summary": "User asked about Python asyncio patterns", "cursor": 1},
        {"summary": "Discussion on FastAPI middleware design", "cursor": 2},
        {"summary": "Tips for Docker multi-stage builds", "cursor": 3},
        {"summary": "React hooks best practices overview", "cursor": 4},
        {"summary": "Database indexing strategies for PostgreSQL", "cursor": 5},
    ]


@pytest.mark.asyncio
async def test_keyword_search_exact_match(sample_entries):
    searcher = KeywordHistorySearch()
    results = await searcher.search(sample_entries, "Python asyncio", limit=3)
    assert len(results) == 1
    assert results[0]["cursor"] == 1


@pytest.mark.asyncio
async def test_keyword_search_multiple_matches(sample_entries):
    searcher = KeywordHistorySearch()
    # "design" matches FastAPI middleware (2), "patterns" matches Python asyncio (1)
    results = await searcher.search(sample_entries, "design patterns", limit=3)
    assert len(results) == 2
    cursors = {r["cursor"] for r in results}
    assert 1 in cursors
    assert 2 in cursors


@pytest.mark.asyncio
async def test_keyword_search_no_match_returns_empty(sample_entries):
    searcher = KeywordHistorySearch()
    results = await searcher.search(sample_entries, "machine learning tensorflow", limit=3)
    assert results == []


@pytest.mark.asyncio
async def test_keyword_search_fallback_to_recent_when_no_keywords():
    searcher = KeywordHistorySearch()
    entries = [
        {"summary": "First entry", "cursor": 1},
        {"summary": "Second entry", "cursor": 2},
        {"summary": "Third entry", "cursor": 3},
    ]
    # Query with only stop words should fall back to recent
    results = await searcher.search(entries, "the a is", limit=2)
    assert len(results) == 2
    assert results[0]["cursor"] == 2
    assert results[1]["cursor"] == 3


@pytest.mark.asyncio
async def test_keyword_search_limit_respected(sample_entries):
    searcher = KeywordHistorySearch()
    # "for" is a stop word, so this falls back to recent
    results = await searcher.search(sample_entries, "for", limit=2)
    assert len(results) == 2


@pytest.mark.asyncio
async def test_keyword_search_chinese_query():
    searcher = KeywordHistorySearch()
    entries = [
        {"summary": "用户询问了Python异步编程的模式", "cursor": 1},
        {"summary": "讨论了FastAPI中间件的设计", "cursor": 2},
        {"summary": "介绍了Docker多阶段构建的技巧", "cursor": 3},
    ]
    results = await searcher.search(entries, "Python 异步", limit=3)
    assert len(results) == 1
    assert results[0]["cursor"] == 1


@pytest.mark.asyncio
async def test_keyword_search_empty_entries():
    searcher = KeywordHistorySearch()
    results = await searcher.search([], "any query", limit=5)
    assert results == []


@pytest.mark.asyncio
async def test_keyword_search_empty_query_returns_recent(sample_entries):
    searcher = KeywordHistorySearch()
    results = await searcher.search(sample_entries, "", limit=2)
    assert len(results) == 2
    assert results[-1]["cursor"] == 5


@pytest.mark.asyncio
async def test_keyword_search_scores_ranked_correctly():
    searcher = KeywordHistorySearch()
    entries = [
        {"summary": "Python async patterns and asyncio", "cursor": 1},
        {"summary": "Python basics for beginners", "cursor": 2},
        {"summary": "Advanced asyncio patterns in Python framework", "cursor": 3},
    ]
    results = await searcher.search(entries, "Python asyncio patterns framework", limit=3)
    assert len(results) == 3
    # Entry 3 matches all 4 keywords, Entry 1 matches 3, Entry 2 matches 1
    assert results[0]["cursor"] == 3
    assert results[1]["cursor"] == 1
    assert results[2]["cursor"] == 2
