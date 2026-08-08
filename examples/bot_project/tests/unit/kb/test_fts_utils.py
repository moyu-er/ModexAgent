"""Tests for bot.kb.fts_utils.sanitize_fts_query.

Verifies FTS5 query sanitization per DESIGN.md §9:
- empty/whitespace queries return ""
- FTS5-special-char-only queries return ""
- tokens are quote-wrapped and OR-joined
- English stopwords are filtered
- CJK text passes through (trigram tokenizer handles CJK natively)
- queries are truncated to 2048 chars before processing
- mixed CJK + English tokenizes correctly
"""

from __future__ import annotations

from bot.kb.fts_utils import sanitize_fts_query


# ── Empty / whitespace → "" ───────────────────────────────────────────────


def test_empty_query_returns_empty_string() -> None:
    assert sanitize_fts_query("") == ""


def test_whitespace_only_query_returns_empty_string() -> None:
    assert sanitize_fts_query("   \t\n  ") == ""


# ── FTS5 special chars → "" ───────────────────────────────────────────────


def test_fts5_special_chars_only_returns_empty_string() -> None:
    # All FTS5 special chars: "()*^:-+
    assert sanitize_fts_query('"()*^:-+') == ""


# ── OR-join structure ─────────────────────────────────────────────────────


def test_or_joins_quoted_tokens() -> None:
    assert sanitize_fts_query("hello world") == '"hello" OR "world"'


# ── Stopword filtering ────────────────────────────────────────────────────


def test_filters_english_stopwords() -> None:
    # "the" is a stopword; "quick", "brown", "fox" survive
    assert sanitize_fts_query("the quick brown fox") == '"quick" OR "brown" OR "fox"'


# ── CJK passthrough ───────────────────────────────────────────────────────


def test_cjk_passthrough_single_token() -> None:
    # CJK has no spaces — entire string is one token, trigram handles it
    assert sanitize_fts_query("部署流程") == '"部署流程"'


# ── Length truncation ─────────────────────────────────────────────────────


def test_truncates_query_to_2048_chars() -> None:
    # 3000-char single token → truncated to 2048 before processing
    long_query = "x" * 3000
    result = sanitize_fts_query(long_query)
    assert result == '"' + "x" * 2048 + '"'


# ── Mixed CJK + English ───────────────────────────────────────────────────


def test_mixed_cjk_and_english() -> None:
    assert sanitize_fts_query("部署 deploy 流程") == '"部署" OR "deploy" OR "流程"'


# ── Punctuation stripping ─────────────────────────────────────────────────


def test_strips_surrounding_punctuation() -> None:
    # Punctuation around tokens is stripped before quoting
    assert sanitize_fts_query("(hello), world!") == '"hello" OR "world"'
