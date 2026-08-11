"""Message-track persistence contract and built-in backends."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Iterable
from pathlib import Path
from sqlite3 import Row
from typing import TYPE_CHECKING, Final

from pydantic import BaseModel, ConfigDict

from modex_agent.core.scope import RecordScope

from .models import MessageTrack, MessageTrackStatus

if TYPE_CHECKING:
    from modex_agent.persistence.connection import ConnectionManager

__all__ = [
    "InMemoryMessageTrackStore",
    "LocalFileMessageTrackStore",
    "MessageTrackStore",
    "SqliteMessageTrackStore",
]

_TRACK_COLUMNS: Final = (
    "track_id, tree_id, message_id, message_type, invocation_id, "
    "target_session_id, source_session_id, status, dispatched_at, consumed_at"
)


class MessageTrackStore(ABC):
    """Persistence interface for routed-message delivery tracks."""

    @abstractmethod
    async def create(self, track: MessageTrack) -> None:
        """Insert a new message track."""
        ...

    @abstractmethod
    async def get(self, track_id: str) -> MessageTrack | None:
        """Fetch a track by its identifier."""
        ...

    @abstractmethod
    async def get_by_message_id(
        self, tree_id: str, message_id: str
    ) -> MessageTrack | None:
        """Fetch a track by message id within one tree."""
        ...

    @abstractmethod
    async def update_status(
        self,
        track_id: str,
        status: MessageTrackStatus,
        consumed_at: int | None = None,
    ) -> None:
        """Update delivery status and optionally its consumption time."""
        ...

    @abstractmethod
    async def has_dispatched(self, tree_id: str) -> bool:
        """Return whether a tree has at least one dispatched track."""
        ...

    @abstractmethod
    async def list_dispatched(self, tree_id: str) -> list[MessageTrack]:
        """List all dispatched tracks for a tree."""
        ...

    @abstractmethod
    async def close_tracks_for_session(
        self, session_id: str, status: MessageTrackStatus
    ) -> None:
        """Close every dispatched track targeting a session."""
        ...


def _updated_track(
    track: MessageTrack,
    status: MessageTrackStatus,
    consumed_at: int | None,
) -> MessageTrack:
    changes: dict[str, MessageTrackStatus | int] = {"status": status}
    if consumed_at is not None:
        changes["consumed_at"] = consumed_at
    return track.model_copy(update=changes)


def _list_dispatched(
    tracks: Iterable[MessageTrack], tree_id: str
) -> list[MessageTrack]:
    return sorted(
        (
            track
            for track in tracks
            if track.tree_id == tree_id
            and track.status is MessageTrackStatus.DISPATCHED
        ),
        key=lambda track: track.track_id,
    )


def _close_dispatched(
    tracks: dict[str, MessageTrack],
    session_id: str,
    status: MessageTrackStatus,
) -> dict[str, MessageTrack]:
    return {
        track_id: _updated_track(track, status, None)
        if track.target_session_id == session_id
        and track.status is MessageTrackStatus.DISPATCHED
        else track
        for track_id, track in tracks.items()
    }


class _MessageTrackFile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tracks: tuple[MessageTrack, ...]


class _MappingMessageTrackStore(MessageTrackStore, ABC):
    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    @abstractmethod
    async def _load_tracks(self) -> dict[str, MessageTrack]: ...

    @abstractmethod
    async def _save_tracks(self, tracks: dict[str, MessageTrack]) -> None: ...

    async def create(self, track: MessageTrack) -> None:
        async with self._lock:
            tracks = await self._load_tracks()
            tracks[track.track_id] = track
            await self._save_tracks(tracks)

    async def get(self, track_id: str) -> MessageTrack | None:
        async with self._lock:
            tracks = await self._load_tracks()
        return tracks.get(track_id)

    async def get_by_message_id(
        self, tree_id: str, message_id: str
    ) -> MessageTrack | None:
        track = await self.get(message_id)
        return track if track is not None and track.tree_id == tree_id else None

    async def update_status(
        self,
        track_id: str,
        status: MessageTrackStatus,
        consumed_at: int | None = None,
    ) -> None:
        async with self._lock:
            tracks = await self._load_tracks()
            track = tracks.get(track_id)
            if track is not None:
                tracks[track_id] = _updated_track(track, status, consumed_at)
                await self._save_tracks(tracks)

    async def has_dispatched(self, tree_id: str) -> bool:
        return bool(await self.list_dispatched(tree_id))

    async def list_dispatched(self, tree_id: str) -> list[MessageTrack]:
        async with self._lock:
            tracks = await self._load_tracks()
        return _list_dispatched(tracks.values(), tree_id)

    async def close_tracks_for_session(
        self, session_id: str, status: MessageTrackStatus
    ) -> None:
        async with self._lock:
            tracks = await self._load_tracks()
            await self._save_tracks(_close_dispatched(tracks, session_id, status))


class InMemoryMessageTrackStore(_MappingMessageTrackStore):
    """Event-loop-local message-track storage for tests and ephemeral use."""

    def __init__(self) -> None:
        super().__init__()
        self._tracks: dict[str, MessageTrack] = {}

    async def _load_tracks(self) -> dict[str, MessageTrack]:
        return self._tracks

    async def _save_tracks(self, tracks: dict[str, MessageTrack]) -> None:
        self._tracks = tracks


class LocalFileMessageTrackStore(_MappingMessageTrackStore):
    """JSON-backed message-track storage with blocking I/O offloaded to threads."""

    def __init__(self, root: Path) -> None:
        super().__init__()
        self._path = root / "message_tracks.json"

    def _read_file(self) -> dict[str, MessageTrack]:
        if not self._path.exists():
            return {}
        payload = _MessageTrackFile.model_validate_json(
            self._path.read_text(encoding="utf-8")
        )
        return {track.track_id: track for track in payload.tracks}

    def _write_file(self, tracks: dict[str, MessageTrack]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = _MessageTrackFile(tracks=tuple(tracks.values())).model_dump_json()
        temporary_path = self._path.with_suffix(".tmp")
        temporary_path.write_text(payload, encoding="utf-8")
        temporary_path.replace(self._path)

    async def _load_tracks(self) -> dict[str, MessageTrack]:
        return await asyncio.to_thread(self._read_file)

    async def _save_tracks(self, tracks: dict[str, MessageTrack]) -> None:
        await asyncio.to_thread(self._write_file, tracks)


class SqliteMessageTrackStore(MessageTrackStore):
    """SQLite-backed message-track storage scoped to one persistence owner."""

    def __init__(self, connection: ConnectionManager, scope: RecordScope) -> None:
        self._connection = connection
        self._scope = scope
        self._owner_scope_key = scope.canonical()

    async def create(self, track: MessageTrack) -> None:
        target_scope = self._scope.model_copy(update={"session_id": track.target_session_id})
        scope_key = target_scope.canonical()
        await self._connection.execute(
            "INSERT INTO message_tracks ("
            "track_id, tree_id, message_id, message_type, invocation_id, "
            "target_session_id, source_session_id, status, scope_key, "
            "owner_scope_key, dispatched_at, consumed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                track.track_id,
                track.tree_id,
                track.message_id,
                track.message_type,
                track.invocation_id,
                track.target_session_id,
                track.source_session_id,
                track.status.value,
                scope_key,
                self._owner_scope_key,
                track.dispatched_at,
                track.consumed_at,
            ),
        )

    async def get(self, track_id: str) -> MessageTrack | None:
        row = await self._connection.query_one(
            f"SELECT {_TRACK_COLUMNS} FROM message_tracks "
            "WHERE track_id = ? AND owner_scope_key = ?",
            (track_id, self._owner_scope_key),
        )
        return self._row_to_track(row) if row is not None else None

    async def get_by_message_id(
        self, tree_id: str, message_id: str
    ) -> MessageTrack | None:
        row = await self._connection.query_one(
            f"SELECT {_TRACK_COLUMNS} FROM message_tracks "
            "WHERE tree_id = ? AND message_id = ? AND owner_scope_key = ?",
            (tree_id, message_id, self._owner_scope_key),
        )
        return self._row_to_track(row) if row is not None else None

    async def update_status(
        self,
        track_id: str,
        status: MessageTrackStatus,
        consumed_at: int | None = None,
    ) -> None:
        await self._connection.execute(
            "UPDATE message_tracks SET status = ?, "
            "consumed_at = COALESCE(?, consumed_at) "
            "WHERE track_id = ? AND owner_scope_key = ?",
            (status.value, consumed_at, track_id, self._owner_scope_key),
        )

    async def has_dispatched(self, tree_id: str) -> bool:
        row = await self._connection.query_one(
            "SELECT 1 FROM message_tracks WHERE tree_id = ? "
            "AND status = 'dispatched' AND owner_scope_key = ? LIMIT 1",
            (tree_id, self._owner_scope_key),
        )
        return row is not None

    async def list_dispatched(self, tree_id: str) -> list[MessageTrack]:
        rows = await self._connection.query_all(
            f"SELECT {_TRACK_COLUMNS} FROM message_tracks WHERE tree_id = ? "
            "AND status = 'dispatched' AND owner_scope_key = ? ORDER BY track_id",
            (tree_id, self._owner_scope_key),
        )
        return [self._row_to_track(row) for row in rows]

    async def close_tracks_for_session(
        self, session_id: str, status: MessageTrackStatus
    ) -> None:
        await self._connection.execute(
            "UPDATE message_tracks SET status = ? WHERE target_session_id = ? "
            "AND status = 'dispatched' AND owner_scope_key = ?",
            (status.value, session_id, self._owner_scope_key),
        )

    @staticmethod
    def _row_to_track(row: Row) -> MessageTrack:
        return MessageTrack.model_validate(dict(row))
