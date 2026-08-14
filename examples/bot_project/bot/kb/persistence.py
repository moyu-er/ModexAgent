"""KbPersistence — abstract base class for KB store persistence.

Responsible for data storage and CRUD. FTS5 index synchronization (triggers)
is a storage-layer side effect — persistence owns the triggers; the retriever
only reads the FTS index.

All methods accept KbFilter for multi-dimensional isolation filtering. A
filter dimension of None means global search across all values for that
dimension.

Backends are replaceable:
  - SqliteKbPersistence: FTS5 + async aiosqlite (current)
  - (future) PostgresKbPersistence, InMemoryKbPersistence, ...

Mirror pattern: TranscriptStore(ABC) at bot/webui/transcript_store.py:41.

KbPersistence intentionally has NO search method — search is the retriever's
responsibility (bot/kb/retriever.py). Hermes' MemoryStore.search_facts leaked
search logic into the persistence layer and caused a circular import; this
design prevents that at the ABC level.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from bot.kb.models import KbEntry, KbFilter, KbUpsertRequest


class KbPersistence(ABC):
    """Abstract persistence layer for the KB store.

    Responsible for data storage and CRUD. FTS5 index synchronization
    (triggers) is a storage-layer side effect — persistence owns the
    triggers; the retriever only reads the FTS index.

    All methods accept KbFilter for multi-dimensional isolation filtering.
    A filter dimension of None means global search across all values for
    that dimension.

    Backends are replaceable:
      - SqliteKbPersistence: FTS5 + async aiosqlite (current)
      - (future) PostgresKbPersistence, InMemoryKbPersistence, ...

    Mirror pattern: TranscriptStore(ABC) at bot/webui/transcript_store.py:41.
    """

    @abstractmethod
    async def upsert(self, request: KbUpsertRequest) -> KbEntry:
        """Write or update an entry (upsert semantics).

        ON CONFLICT(task_id, session_id, key) DO UPDATE. Returns the updated KbEntry
        (including entry_id). FTS5 triggers auto-sync the index.
        """
        ...

    @abstractmethod
    async def get(self, key: str, filter: KbFilter) -> KbEntry | None:
        """Exact read. filter narrows the search scope."""
        ...

    @abstractmethod
    async def delete(self, key: str, filter: KbFilter) -> bool:
        """Delete an entry. Returns whether the delete succeeded."""
        ...

    @abstractmethod
    async def list_keys(
        self,
        filter: KbFilter,
        prefix: str | None = None,
    ) -> list[str]:
        """List keys. prefix does optional prefix matching."""
        ...
