"""KbRetriever -- the retrieval half of the KB store decoupling.

KbRetriever owns search and ranking. It shares the persistence
ConnectionManager but executes its own SQL directly -- search queries
(FTS5 MATCH, vector cosine, etc.) are retrieval-strategy-specific and do
not belong on the persistence interface.

Retrieval strategy is decided by the concrete backend (FTS5 / vector /
hybrid / ReAct, etc.). See DESIGN.md section 5 for backend variants.

Mirror pattern: KbPersistence(ABC) at bot/kb/persistence.py.
See DESIGN.md section 5 for the full design rationale.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from bot.kb.models import KbFilter, KbSearchResult


class KbRetriever(ABC):
    """Knowledge-base retrieval abstraction.

    Responsible for search. The retrieval strategy is decided by the
    concrete backend (FTS5 / vector / hybrid / ReAct). The retriever
    shares persistence's ConnectionManager and executes search queries
    directly -- it does not go through the persistence interface, because
    search SQL / FTS5 syntax is retrieval-strategy-specific.

    All methods accept KbFilter for multi-dimensional isolation filtering.
    """

    @abstractmethod
    async def search(
        self,
        query: str,
        filter: KbFilter,
        limit: int = 20,
    ) -> list[KbSearchResult]:
        """Search the knowledge base.

        The backend decides the retrieval strategy and scoring algorithm.
        Returns results with scores, ordered by score descending. Score
        semantics are defined by the backend (BM25 rank / cosine / blend).
        """
        ...
