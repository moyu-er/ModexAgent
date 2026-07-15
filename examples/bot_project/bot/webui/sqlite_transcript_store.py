"""SQLite adapter for bot-owned WebUI transcript events."""

from __future__ import annotations

import json
import sqlite3
from sqlite3 import Row
from typing import TYPE_CHECKING, Final

from bot.webui.events import ServerEvent
from bot.webui.transcript_store import (
    MaterializedTurn,
    TranscriptPersistenceError,
    TranscriptStore,
    _materialize_events,
)
from modex_agent.core.session_id import session_id_prefix_of

if TYPE_CHECKING:
    from modex_agent.persistence.connection import ConnectionManager

_SELECT_EVENT: Final = "SELECT payload_json FROM bot_webui_transcript_events"


class SqliteTranscriptStore(TranscriptStore):
    """Append-only transcript adapter borrowing one workspace connection."""

    def __init__(self, connection: ConnectionManager) -> None:
        self._connection = connection

    async def append(
        self,
        session_id: str,
        event: ServerEvent,
        *,
        pool: str = "main",
    ) -> None:
        if event.session_id != session_id:
            raise ValueError("event session_id does not match transcript key")
        payload = json.dumps(event.to_dict(), ensure_ascii=False, separators=(",", ":"))
        turn_id = event.to_dict().get("turn_id")
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
                    pool,
                    event.agent_name,
                    event.event,
                    str(turn_id) if turn_id else None,
                    event.timestamp,
                    payload,
                ),
            )
        except sqlite3.Error as exc:
            raise TranscriptPersistenceError from exc

    async def load(self, session_id: str) -> list[ServerEvent]:
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
    ) -> list[ServerEvent]:
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
        return _materialize_events(
            await self.load_sessions_by_prefix(session_prefix, pool=pool)
        )


def _decode(rows: list[Row]) -> list[ServerEvent]:
    return [ServerEvent.from_dict(json.loads(str(row[0]))) for row in rows]
