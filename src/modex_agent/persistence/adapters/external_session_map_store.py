"""SQLite-backed :class:`~modex_agent.agents.external_coding.session_store.ExternalSessionMapStore`.

Stores the Modex→provider session mapping in the ``external_session_map``
table. ``resolve`` is sync per the ABC contract — it opens a short-lived
``sqlite3`` connection to the same WAL-mode database (safe for concurrent
reads alongside async writes through the ConnectionManager, mirroring the
pattern established by :class:`~modex_agent.persistence.adapters.inbox_mq.SqliteInboxMQ`).
``commit`` and ``invalidate`` go through the async ``ConnectionManager``.

The ``invalidated`` column (INTEGER CHECK 0/1) implements soft-delete
(matching
:class:`~modex_agent.agents.external_coding.session_store.LocalFileExternalSessionMapStore`):
``invalidate`` sets ``invalidated = 1``; ``resolve`` treats invalidated
entries as absent — returning ``(None, False)``. ``last_committed_at`` is
stored as integer milliseconds (ADR-0029 §2); ``created_at``/``updated_at``
are owned by the schema DEFAULT + the
``trg_external_session_map_auto_updated_at`` trigger.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

from modex_agent.agents.external_coding.paths import ProviderKind
from modex_agent.agents.external_coding.session_store import ExternalSessionMapStore
from modex_agent.utils.time import now_ms

if TYPE_CHECKING:
    from modex_agent.core.scope import RecordScope
    from modex_agent.persistence.connection import ConnectionManager


class SqliteExternalSessionMapStore(ExternalSessionMapStore):
    """SQLite-backed session map using the ``external_session_map`` table.

    The ``scope_key`` column is populated from the injected
    :class:`RecordScope`'s canonical JSON.

    Args:
        connection: The workspace ``ConnectionManager`` shared with other
            adapters. Used for async writes (``commit``, ``invalidate``).
        scope: A ``RecordScope`` whose canonical JSON populates the
            ``scope_key`` column.
    """

    def __init__(self, connection: ConnectionManager, scope: RecordScope) -> None:
        self._connection = connection
        # Sync reads (resolve) need a direct sqlite3 connection because the
        # ABC mandates a sync signature while ConnectionManager is async.
        # The db_path is captured once at construction; it does not change
        # over the ConnectionManager's lifetime.
        self._db_path = connection._db_path
        self._scope_json = scope.canonical()

    def resolve(self, modex_session_id: str) -> tuple[str | None, bool]:
        """Look up the provider session id for a modex session.

        Opens a short-lived ``sqlite3`` reader. WAL mode allows this to
        coexist safely with async writes through the ConnectionManager.

        Returns ``(provider_session_id, True)`` if a non-invalidated entry
        exists; ``(None, False)`` otherwise.
        """
        conn = sqlite3.connect(str(self._db_path), isolation_level=None)
        try:
            conn.execute("PRAGMA busy_timeout = 5000")
            row = conn.execute(
                "SELECT provider_session_id, invalidated "
                "FROM external_session_map WHERE modex_session_id = ?",
                (modex_session_id,),
            ).fetchone()
            if row is None or row[1]:
                return (None, False)
            return (row[0], True)
        finally:
            conn.close()

    async def commit(
        self,
        modex_session_id: str,
        provider_session_id: str,
        provider_kind: ProviderKind,
    ) -> None:
        """Persist or replace a provider session mapping.

        Upserts the row by ``modex_session_id`` (PK). Resets
        ``invalidated = 0`` so a re-commit after invalidate reactivates
        the entry. ``last_committed_at`` is written as int ms (ADR-0029 §2);
        ``updated_at`` is owned by the auto-update trigger.
        """
        await self._connection.execute(
            "INSERT INTO external_session_map "
            "(modex_session_id, provider_session_id, provider_kind, scope_key, "
            "last_committed_at, invalidated) "
            "VALUES (?, ?, ?, ?, ?, 0) "
            "ON CONFLICT(modex_session_id) DO UPDATE SET "
            "provider_session_id = excluded.provider_session_id, "
            "provider_kind = excluded.provider_kind, "
            "last_committed_at = excluded.last_committed_at, "
            "invalidated = 0",
            (
                modex_session_id,
                provider_session_id,
                provider_kind.value,
                self._scope_json,
                now_ms(),
            ),
        )

    async def invalidate(self, modex_session_id: str) -> None:
        """Mark a mapping as invalidated so the next resolve is fresh.

        No-op if no entry exists for ``modex_session_id``.
        """
        await self._connection.execute(
            "UPDATE external_session_map SET invalidated = 1 WHERE modex_session_id = ?",
            (modex_session_id,),
        )
