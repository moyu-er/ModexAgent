"""Transcript store for WebUI conversations.

The store is keyed by the **full session id** — the same receiver-owned
identifier the memory system uses (``{conversation_id}.{agent_name}`` for main
agents, ``{conversation_id}.{agent_name}.{invocation_id}`` for subagents).
Using the full session id as the persistence key means two subagent
invocations of the same agent (e.g. two ``reviewer`` runs) never collapse into
one transcript file.

The conversation prefix (everything before the first ``.``) is the user-facing
grouping: a UI conversation owns many sessions (the main agent + each
subagent invocation).  ``load_conversation`` merges them by timestamp.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Iterator

from framework.core.session_id import snowflake_of
from framework.core.session_store import safe_filename

from bot.webui.events import ServerEvent


def _conversation_prefix(session_id: str) -> str:
    """Return the conversation prefix (segment before the first ``.``).

    ``"abc.main"`` → ``"abc"``; ``"abc.reviewer.z9"`` → ``"abc"``.
    Delegates to :func:`framework.core.session_id.snowflake_of`.
    """
    return snowflake_of(session_id)


class TranscriptStore(ABC):
    """Abstract transcript store keyed by the full session id."""

    @abstractmethod
    def append(self, session_id: str, event: ServerEvent) -> None:
        """Persist a single event for *session_id* (full session identifier)."""
        ...

    @abstractmethod
    def load(self, session_id: str) -> Iterator[ServerEvent]:
        """Yield all events for *session_id* (full session identifier), oldest first."""
        ...

    @abstractmethod
    def load_conversation(self, conversation_id: str) -> Iterator[ServerEvent]:
        """Yield events from every session in *conversation_id*, merged by timestamp."""
        ...

    @abstractmethod
    def list_sessions(self) -> set[str]:
        """Return the set of all full session ids that have at least one event."""
        ...

    @abstractmethod
    def list_sessions_in_conversation(self, conversation_id: str) -> set[str]:
        """Return the set of full session ids owned by *conversation_id*."""
        ...

    @abstractmethod
    def delete_session(self, session_id: str) -> None:
        """Remove all records for one full *session_id*."""
        ...

    @abstractmethod
    def delete_conversation(self, conversation_id: str) -> None:
        """Remove all records for every session in *conversation_id*."""
        ...

    def last_updated(self, session_id: str) -> int | None:
        """Return the last update timestamp for *session_id* in milliseconds, or None.

        The default implementation returns ``None``; concrete stores should
        override this with an efficient lookup (e.g. file mtime or the newest
        event timestamp).
        """
        return None


# ── JSONL implementation ───────────────────────────────────────────────────


class JSONLTranscriptStore(TranscriptStore):
    """Stores events as one JSONL file per full session id.

    File layout: ``base_dir/{safe_session_id}.jsonl`` where *session_id* is the
    full receiver-owned identifier (``{conv}.{agent}[.{invocation_id}]``).
    """

    def __init__(self, base_dir: Path) -> None:
        self._base_dir = base_dir

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _file_for(self, session_id: str) -> Path:
        return self._base_dir / f"{safe_filename(session_id)}.jsonl"

    def _iter_files(self) -> Iterator[Path]:
        if not self._base_dir.is_dir():
            return
        for f in self._base_dir.iterdir():
            if f.is_file() and f.suffix == ".jsonl":
                yield f

    def _session_id_of(self, path: Path) -> str:
        """Reverse the safe-name mapping for a file stem.

        ``_safe_name`` only rewrites ``:`` and ``/``; since session ids use
        ``.`` as the only separator and never contain those chars after
        sanitization, the stem is the session id.
        """
        return path.stem

    # ------------------------------------------------------------------
    # TranscriptStore interface
    # ------------------------------------------------------------------

    def append(self, session_id: str, event: ServerEvent) -> None:
        self._base_dir.mkdir(parents=True, exist_ok=True)
        line = json.dumps(event.to_dict(), ensure_ascii=False)
        with self._file_for(session_id).open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    def load(self, session_id: str) -> Iterator[ServerEvent]:
        file_path = self._file_for(session_id)
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

    def load_conversation(self, conversation_id: str) -> Iterator[ServerEvent]:
        all_events: list[tuple[float, ServerEvent]] = []
        for session_id in self.list_sessions_in_conversation(conversation_id):
            for event in self.load(session_id):
                all_events.append((event.timestamp, event))
        all_events.sort(key=lambda pair: pair[0])
        for _, event in all_events:
            yield event

    def list_sessions(self) -> set[str]:
        seen: set[str] = set()
        for f in self._iter_files():
            seen.add(self._session_id_of(f))
        return seen

    def list_sessions_in_conversation(self, conversation_id: str) -> set[str]:
        prefix = safe_filename(conversation_id) + "."
        seen: set[str] = set()
        for f in self._iter_files():
            stem = self._session_id_of(f)
            if _conversation_prefix(stem) == safe_filename(conversation_id) or stem.startswith(prefix):
                seen.add(stem)
        return seen

    def delete_session(self, session_id: str) -> None:
        self._file_for(session_id).unlink(missing_ok=True)

    def delete_conversation(self, conversation_id: str) -> None:
        for session_id in self.list_sessions_in_conversation(conversation_id):
            self.delete_session(session_id)

    def last_updated(self, session_id: str) -> int | None:
        """Return transcript file mtime in milliseconds, or None if no file."""
        file_path = self._file_for(session_id)
        if not file_path.is_file():
            return None
        return int(file_path.stat().st_mtime * 1000)
