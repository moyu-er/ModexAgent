"""Persistence-owned transcript adapter construction."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path

from bot.persistence.migration import BotWorkspaceMigrationRunner
from bot.webui.sqlite_transcript_store import SqliteTranscriptStore
from bot.webui.transcript_store import ResilientTranscriptStore, TranscriptStore
from modex_agent.persistence.connection import ConnectionManager
from modex_agent.plugins.persistence_backends import FILE_BACKEND_NAME, SQLITE_BACKEND_NAME

TranscriptStoreResolver = Callable[[Path], Awaitable[TranscriptStore]]


async def prepare_database_transcript(connection: ConnectionManager) -> None:
    """Prepare transcript schema for the configured workspace database adapter."""
    await BotWorkspaceMigrationRunner(connection).run_pending()


async def build_database_transcript_store(
    connection: ConnectionManager,
) -> TranscriptStore:
    """Prepare bot schema and adapt a workspace database to transcript storage."""
    await prepare_database_transcript(connection)
    return ResilientTranscriptStore(SqliteTranscriptStore(connection))


def build_transcript_store_resolver(
    backend: str,
    database_resolver: TranscriptStoreResolver,
) -> TranscriptStoreResolver | None:
    """Select transcript persistence without leaking providers into callers."""
    if backend == FILE_BACKEND_NAME:
        return None
    if backend == SQLITE_BACKEND_NAME:
        return database_resolver
    raise ValueError(
        f"unknown persistence backend {backend!r} for transcript selection "
        f"(expected {FILE_BACKEND_NAME!r} or {SQLITE_BACKEND_NAME!r})"
    )
