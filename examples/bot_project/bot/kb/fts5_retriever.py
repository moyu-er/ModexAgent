from __future__ import annotations

from bot.kb.fts_utils import sanitize_fts_query
from bot.kb.models import KbEntry, KbFilter, KbSearchResult
from bot.kb.retriever import KbRetriever
from bot.kb.sqlite_utils import build_filter_clauses
from modex_agent.persistence.connection import ConnectionManager


class Fts5Retriever(KbRetriever):
    """Search KB entries with FTS5 trigram matching and BM25 ranking."""

    def __init__(self, connection: ConnectionManager) -> None:
        self._conn = connection

    async def search(
        self,
        query: str,
        filter: KbFilter,
        limit: int = 20,
    ) -> list[KbSearchResult]:
        sanitized = sanitize_fts_query(query)
        if not sanitized:
            return []

        where_clauses, filter_params = build_filter_clauses(
            filter,
            alias="e.",
        )
        where_clauses.insert(0, "kb_entries_fts MATCH ?")
        params: list[str | int] = [sanitized]
        params.extend(filter_params)
        params.append(limit)

        sql = f"""
            SELECT e.entry_id, e.key, e.value, e.task_id, e.session_id,
                   e.category, e.tags, e.created_at, e.updated_at,
                   kb_entries_fts.rank AS fts_rank
            FROM kb_entries_fts
            JOIN kb_entries e ON e.entry_id = kb_entries_fts.rowid
            WHERE {" AND ".join(where_clauses)}
            ORDER BY kb_entries_fts.rank
            LIMIT ?
        """
        rows = await self._conn.query_all(sql, tuple(params))

        results: list[KbSearchResult] = []
        for row in rows:
            entry_data = dict(row)
            rank = entry_data.pop("fts_rank", 0.0)
            score = abs(float(rank)) if rank else 0.0
            results.append(KbSearchResult(entry=KbEntry(**entry_data), score=score))
        return results
