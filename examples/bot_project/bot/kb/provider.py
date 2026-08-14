"""KbProvider — facade composing KbPersistence + KbRetriever.

Consumers (KbTool / CLI / REST route) depend only on KbProvider, never on
persistence or retriever directly. search() delegates to the retriever; all
other methods delegate to persistence. Per DESIGN.md §6.

KbPersistence / KbRetriever are imported under TYPE_CHECKING because the
provider is pure delegation — it never instantiates or isinstance-checks the
backends, so it does not need them at runtime. With ``from __future__ import
annotations`` the annotations are lazy strings and resolve at type-check time
once the concrete ABCs land (T5/T6).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from bot.kb.models import KbEntry, KbFilter, KbSearchResult, KbUpsertRequest

if TYPE_CHECKING:
    from bot.kb.persistence import KbPersistence
    from bot.kb.retriever import KbRetriever


class KbProvider:
    """Knowledge-base facade. Composes KbPersistence + KbRetriever.

    Consumers (KbTool / CLI / REST route) depend only on KbProvider,
    not on persistence or retriever directly.

    Assembly point composes the two backends::

        provider = KbProvider(
            persistence=SqliteKbPersistence(conn),
            retriever=Fts5Retriever(conn),
        )

    Switching retrieval strategy (persistence untouched)::

        provider = KbProvider(
            persistence=same_sqlite_persistence,
            retriever=alternative_retriever,
        )
    """

    def __init__(
        self,
        persistence: KbPersistence,
        retriever: KbRetriever,
    ) -> None:
        self._persistence = persistence
        self._retriever = retriever

    async def upsert(self, request: KbUpsertRequest) -> KbEntry:
        return await self._persistence.upsert(request)

    async def get(self, key: str, filter: KbFilter) -> KbEntry | None:
        return await self._persistence.get(key, filter)

    async def delete(self, key: str, filter: KbFilter) -> bool:
        return await self._persistence.delete(key, filter)

    async def list_keys(
        self, filter: KbFilter, prefix: str | None = None,
    ) -> list[str]:
        return await self._persistence.list_keys(filter, prefix)

    async def search(
        self, query: str, filter: KbFilter, limit: int = 20,
    ) -> list[KbSearchResult]:
        return await self._retriever.search(query, filter, limit)
