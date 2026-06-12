"""Transcript store for WebUI conversations.

Two implementations:
- :class:`JSONLTranscriptStore` — filesystem-based, one JSONL file per agent.
- :class:`SQLiteTranscriptStore` — single-file SQLite database.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Iterator

from bot.webui.events import ServerEvent


def _safe_name(name: str) -> str:
    """Replace path-unsafe characters with underscores."""
    return name.replace(":", "_").replace("/", "_")


class TranscriptStore(ABC):
    """Abstract transcript store for persisting and retrieving server events."""

    @abstractmethod
    def append(self, conversation_id: str, agent_name: str, event: ServerEvent) -> None:
        """Persist a single event for a conversation/agent pair."""
        ...

    @abstractmethod
    def load(self, conversation_id: str, agent_name: str) -> Iterator[ServerEvent]:
        """Yield all events for a conversation/agent pair, oldest first."""
        ...

    @abstractmethod
    def load_all(self, conversation_id: str) -> Iterator[ServerEvent]:
        """Yield events from ALL agents for a conversation, merged by timestamp."""
        ...

    @abstractmethod
    def list_conversations(self) -> set[str]:
        """Return the set of known conversation IDs."""
        ...

    @abstractmethod
    def list_agents(self, conversation_id: str) -> set[str]:
        """Return the set of agent names within a conversation."""
        ...

    @abstractmethod
    def delete_conversation(self, conversation_id: str) -> None:
        """Remove all records for a conversation."""
        ...


class JSONLTranscriptStore(TranscriptStore):
    """Stores events as JSONL files under ``base_dir/{safe_conv}/{safe_agent}.jsonl``.

    Conversation IDs and agent names are sanitized (``:`` and ``/`` replaced with
    ``_``) to produce safe filesystem paths.
    """

    def __init__(self, base_dir: Path) -> None:
        self._base_dir = base_dir

    # ------------------------------------------------------------------
    # TranscriptStore interface
    # ------------------------------------------------------------------

    def append(self, conversation_id: str, agent_name: str, event: ServerEvent) -> None:
        conv_dir = self._base_dir / _safe_name(conversation_id)
        conv_dir.mkdir(parents=True, exist_ok=True)
        file_path = conv_dir / f"{_safe_name(agent_name)}.jsonl"
        line = json.dumps(event.to_dict(), ensure_ascii=False)
        with file_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    def load(self, conversation_id: str, agent_name: str) -> Iterator[ServerEvent]:
        file_path = self._base_dir / _safe_name(conversation_id) / f"{_safe_name(agent_name)}.jsonl"
        if not file_path.is_file():
            return
        with file_path.open("r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    data = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                yield ServerEvent.from_dict(data)

    def load_all(self, conversation_id: str) -> Iterator[ServerEvent]:
        """Yield events from ALL agents, merged by timestamp."""
        all_events: list[tuple[float, ServerEvent]] = []
        for agent_name in self.list_agents(conversation_id):
            for event in self.load(conversation_id, agent_name):
                all_events.append((event.timestamp, event))
        all_events.sort(key=lambda pair: pair[0])
        for _, event in all_events:
            yield event

    def list_conversations(self) -> set[str]:
        if not self._base_dir.is_dir():
            return set()
        return {
            d.name for d in self._base_dir.iterdir()
            if d.is_dir()
        }

    def list_agents(self, conversation_id: str) -> set[str]:
        conv_dir = self._base_dir / _safe_name(conversation_id)
        if not conv_dir.is_dir():
            return set()
        return {
            f.stem for f in conv_dir.iterdir()
            if f.is_file() and f.suffix == ".jsonl"
        }

    def delete_conversation(self, conversation_id: str) -> None:
        conv_dir = self._base_dir / _safe_name(conversation_id)
        if conv_dir.is_dir():
            shutil.rmtree(conv_dir)


# ── SQLite implementation ──────────────────────────────────────────────────

_CREATE_TABLE_SQL: str = """\
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    agent_name TEXT NOT NULL,
    event_json TEXT NOT NULL,
    created_at REAL NOT NULL
);
"""

_CREATE_INDEX_SQL: str = """\
CREATE INDEX IF NOT EXISTS idx_events_lookup
    ON events(conversation_id, agent_name, id);
"""

_INSERT_SQL: str = (
    "INSERT INTO events (conversation_id, agent_name, event_json, created_at) "
    "VALUES (?, ?, ?, ?)"
)

_LOAD_SQL: str = (
    "SELECT event_json FROM events "
    "WHERE conversation_id = ? AND agent_name = ? "
    "ORDER BY id ASC"
)

_LIST_CONV_SQL: str = "SELECT DISTINCT conversation_id FROM events"

_LIST_AGENTS_SQL: str = (
    "SELECT DISTINCT agent_name FROM events WHERE conversation_id = ?"
)

_DELETE_SQL: str = "DELETE FROM events WHERE conversation_id = ?"


class SQLiteTranscriptStore(TranscriptStore):
    """Stores events in a single-file SQLite database.

    Usage::

        store = SQLiteTranscriptStore(Path(\"data/webui/transcripts.db\"))
        store.append(\"conv1\", \"main\", event)
        for ev in store.load(\"conv1\", \"main\"):
            print(ev.event)
    """

    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db_path: Path = db_path
        self._conn: sqlite3.Connection = sqlite3.connect(str(db_path))
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute(_CREATE_TABLE_SQL)
        self._conn.execute(_CREATE_INDEX_SQL)
        self._conn.commit()

    # ------------------------------------------------------------------
    # TranscriptStore interface
    # ------------------------------------------------------------------

    def append(self, conversation_id: str, agent_name: str, event: ServerEvent) -> None:
        json_str = json.dumps(event.to_dict(), ensure_ascii=False)
        self._conn.execute(
            _INSERT_SQL,
            (conversation_id, agent_name, json_str, time.time()),
        )
        self._conn.commit()

    def load(self, conversation_id: str, agent_name: str) -> Iterator[ServerEvent]:
        rows = self._conn.execute(_LOAD_SQL, (conversation_id, agent_name))
        for (json_str,) in rows:
            try:
                data: dict[str, object] = json.loads(json_str)
            except json.JSONDecodeError:
                continue
            yield ServerEvent.from_dict(data)

    _LOAD_ALL_SQL: str = (
        "SELECT event_json FROM events "
        "WHERE conversation_id = ? "
        "ORDER BY created_at ASC, id ASC"
    )

    def load_all(self, conversation_id: str) -> Iterator[ServerEvent]:
        """Yield events from ALL agents, merged by timestamp."""
        rows = self._conn.execute(self._LOAD_ALL_SQL, (conversation_id,))
        all_events: list[tuple[float, ServerEvent]] = []
        for (json_str,) in rows:
            try:
                data: dict[str, object] = json.loads(json_str)
            except json.JSONDecodeError:
                continue
            event = ServerEvent.from_dict(data)
            all_events.append((event.timestamp, event))
        all_events.sort(key=lambda pair: pair[0])
        for _, event in all_events:
            yield event

    def list_conversations(self) -> set[str]:
        rows = self._conn.execute(_LIST_CONV_SQL)
        return {row[0] for row in rows}

    def list_agents(self, conversation_id: str) -> set[str]:
        rows = self._conn.execute(_LIST_AGENTS_SQL, (conversation_id,))
        return {row[0] for row in rows}

    def delete_conversation(self, conversation_id: str) -> None:
        self._conn.execute(_DELETE_SQL, (conversation_id,))
        self._conn.commit()

    def close(self) -> None:
        """Close the database connection (call on shutdown)."""
        self._conn.close()
