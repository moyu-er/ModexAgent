"""FTS5 query sanitization utility.

Reference: hermes FactRetriever._sanitize_fts_query (retrieval.py:564-619).

Belongs to the retrieval layer (not persistence) — FTS5 query syntax is a
search-strategy concern. hermes's MemoryStore.search_facts deferred-imports
this function causing a circular import; this design places sanitize in a
standalone retrieval-layer module, eliminating the cycle at the ABC level.

The trigram tokenizer natively supports CJK substring matching, so no CJK
routing or dual-table logic is needed (unlike hermes_state.py).
"""

from __future__ import annotations

_MAX_QUERY_CHARS = 2048
_FTS_SPECIAL = '"()*^:-+'

_FTS_STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from",
    "has", "have", "he", "in", "is", "it", "its", "of", "on", "or",
    "that", "the", "this", "to", "was", "were", "will", "with",
})


def sanitize_fts_query(query: str) -> str:
    """Convert a natural-language query into a safe FTS5 OR expression.

    Steps:
    1. Truncate to 2048 chars.
    2. Split by whitespace (CJK runs stay as one token).
    3. Strip FTS5 special chars and surrounding punctuation.
    4. Drop tokens shorter than 2 chars and English stopwords.
    5. Quote-wrap each surviving token.
    6. OR-join for high recall (AND-joining causes zero-recall on misspellings).

    CJK: the trigram tokenizer splits CJK runs into 3-grams automatically,
    so continuous CJK text passes through as one quoted token.
    """
    if not query or not query.strip():
        return ""
    query = query[:_MAX_QUERY_CHARS]

    tokens: list[str] = []
    for raw in query.split():
        cleaned = raw.strip(".,;:!?\"'()[]{}#@<>")
        cleaned = cleaned.translate(str.maketrans("", "", _FTS_SPECIAL))
        if len(cleaned) < 2:
            continue
        if cleaned.lower() in _FTS_STOPWORDS:
            continue
        tokens.append(f'"{cleaned}"')

    return " OR ".join(tokens) if tokens else ""
