"""SQLite adapter for bot-owned WebUI transcript events."""

from __future__ import annotations

import sqlite3
from sqlite3 import Row
from typing import TYPE_CHECKING, Final

from bot.webui.transcript_store import (
    MaterializedTurn,
    TranscriptPersistenceError,
    TranscriptRecord,
    TranscriptRecordCodec,
    TranscriptStore,
    UserMessageRecord,
    materialize_records,
)
from modex_agent.core.session_id import session_id_prefix_of

if TYPE_CHECKING:
    from modex_agent.persistence.connection import ConnectionManager

_SELECT_EVENT: Final = "SELECT payload_json FROM bot_webui_transcript_events"

#: Shared line codec — its ``event_time`` and generation-detecting
#: ``parse`` are the single record decode path for SQLite rows too.
_CODEC: Final = TranscriptRecordCodec()


class SqliteTranscriptStore(TranscriptStore):
    """Append-only transcript adapter borrowing one workspace connection."""

    def __init__(self, connection: ConnectionManager) -> None:
        self._connection = connection

    async def append(
        self,
        session_id: str,
        event: TranscriptRecord,
        *,
        pool: str | None = None,
    ) -> None:
        if event.session_id != session_id:
            raise ValueError("event session_id does not match transcript key")
        payload = event.model_dump_json()
        # The user-message record has no turn identity; every other
        # record (presentation events + attachment carriers) carries one.
        turn_id = None if isinstance(event, UserMessageRecord) else event.turn_id
        try:
            await self._connection.execute(
                """
                INSERT INTO bot_webui_transcript_events (
                    session_id, session_prefix, pool_name, agent_name, event_type,
                    turn_id, timestamp_ms, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    session_id_prefix_of(session_id),
                    pool if pool is not None else "main",
                    event.agent_name,
                    event.kind,
                    str(turn_id) if turn_id else None,
                    _CODEC.event_time(event),
                    payload,
                ),
            )
        except sqlite3.Error as exc:
            raise TranscriptPersistenceError from exc

    async def load(self, session_id: str) -> list[TranscriptRecord]:
        rows = await self._connection.query_all(
            f"{_SELECT_EVENT} WHERE session_id = ? ORDER BY event_id",
            (session_id,),
        )
        return _decode(rows)

    async def load_sessions_by_prefix(
        self,
        session_prefix: str,
        *,
        pool: str | None = None,
    ) -> list[TranscriptRecord]:
        if pool is None:
            rows = await self._connection.query_all(
                f"{_SELECT_EVENT} WHERE session_prefix = ? "
                "ORDER BY timestamp_ms, event_id",
                (session_prefix,),
            )
        else:
            rows = await self._connection.query_all(
                f"{_SELECT_EVENT} WHERE pool_name = ? AND session_prefix = ? "
                "ORDER BY timestamp_ms, event_id",
                (pool, session_prefix),
            )
        return _decode(rows)

    async def list_sessions(self) -> set[str]:
        rows = await self._connection.query_all(
            "SELECT DISTINCT session_id FROM bot_webui_transcript_events"
        )
        return {str(row[0]) for row in rows}

    async def list_sessions_by_prefix(self, session_prefix: str) -> set[str]:
        rows = await self._connection.query_all(
            "SELECT DISTINCT session_id FROM bot_webui_transcript_events "
            "WHERE session_prefix = ?",
            (session_prefix,),
        )
        return {str(row[0]) for row in rows}

    async def delete_session(self, session_id: str) -> None:
        await self._connection.execute(
            "DELETE FROM bot_webui_transcript_events WHERE session_id = ?",
            (session_id,),
        )

    async def delete_sessions_by_prefix(self, session_prefix: str) -> None:
        await self._connection.execute(
            "DELETE FROM bot_webui_transcript_events WHERE session_prefix = ?",
            (session_prefix,),
        )

    async def last_updated(self, session_id: str) -> int | None:
        row = await self._connection.query_one(
            "SELECT MAX(timestamp_ms) FROM bot_webui_transcript_events "
            "WHERE session_id = ?",
            (session_id,),
        )
        if row is None or row[0] is None:
            return None
        return int(row[0])

    async def load_materialized_by_prefix(
        self,
        session_prefix: str,
        *,
        pool: str | None = None,
    ) -> list[MaterializedTurn]:
        return materialize_records(
            await self.load_sessions_by_prefix(session_prefix, pool=pool)
        )


def _decode(rows: list[Row]) -> list[TranscriptRecord]:
    """Decode payload rows through the shared generation-detecting codec.

    Legacy rows (pre-cutover ``ServerEvent`` payloads) convert at read
    time inside the codec — the single conversion point.
    """
    records: list[TranscriptRecord] = []
    for row in rows:
        records.extend(_CODEC.parse(str(row[0])))
    return records
