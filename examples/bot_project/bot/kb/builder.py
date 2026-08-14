"""KB builder resolvers — compose the KB stack from a ConnectionManager.

Mirrors build_database_transcript_store(connection) at
bot/persistence/transcript.py:23. The assembly point (resources.py) calls
build_default_kb_provider(connection) to get a fully composed KbProvider.

Schema is applied by BotWorkspaceMigrationRunner elsewhere in the assembly
flow — these builders only wire concrete backends to the provider facade.
"""

from __future__ import annotations

from bot.kb.fts5_retriever import Fts5Retriever
from bot.kb.persistence import KbPersistence
from bot.kb.provider import KbProvider
from bot.kb.retriever import KbRetriever
from bot.kb.sqlite_persistence import SqliteKbPersistence
from modex_agent.persistence.connection import ConnectionManager


def build_sqlite_kb_persistence(
    connection: ConnectionManager,
) -> KbPersistence:
    """Build the SQLite persistence backend.

    Schema is applied by BotWorkspaceMigrationRunner elsewhere (not here).
    """
    return SqliteKbPersistence(connection)


def build_fts5_retriever(connection: ConnectionManager) -> KbRetriever:
    """Build the FTS5 retrieval backend. Shares persistence's connection."""
    return Fts5Retriever(connection)


def build_kb_provider(
    persistence: KbPersistence,
    retriever: KbRetriever,
) -> KbProvider:
    """Compose persistence + retriever into the KbProvider facade."""
    return KbProvider(persistence=persistence, retriever=retriever)


async def build_default_kb_provider(
    connection: ConnectionManager,
) -> KbProvider:
    """Default composition: SqliteKbPersistence + Fts5Retriever (v3 converged).

    Still async for caller compatibility (resources.py awaits this).
    build_sqlite_kb_persistence is now sync, so this contains no await.
    To switch retrieval strategy later, the assembly point changes to
    build_kb_provider(sqlite_persistence, hybrid_retriever).
    """
    persistence = build_sqlite_kb_persistence(connection)
    retriever = build_fts5_retriever(connection)
    return build_kb_provider(persistence, retriever)
