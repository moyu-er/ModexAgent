"""Transcript store for WebUI conversations.

The store is keyed by the **full session id** — the same receiver-owned
identifier the memory system uses (``{session_prefix}.{agent_name}`` for main
agents, ``{session_prefix}.{agent_name}.{invocation_id}`` for subagents).
Using the full session id as the persistence key means two subagent
invocations of the same agent (e.g. two ``reviewer`` runs) never collapse into
one transcript file.

The session prefix (everything before the first ``.``) is the user-facing
grouping: a UI conversation owns many sessions (the main agent + each
subagent invocation).  ``load_sessions_by_prefix`` merges them by timestamp.
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass as _dataclass, field as _dc_field
from pathlib import Path
from typing import Iterator

from modex_agent.core.session_id import session_id_prefix_of
from modex_agent.core.session_store import safe_filename

from bot.webui.events import ServerEvent

logger = logging.getLogger(__name__)


def _session_id_prefix(session_id: str) -> str:
    """Return the session prefix (segment before the first ``.``).

    ``"abc.main"`` → ``"abc"``; ``"abc.reviewer.z9"`` → ``"abc"``.
    Delegates to :func:`modex_agent.core.session_id.session_id_prefix_of`.
    """
    return session_id_prefix_of(session_id)


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
    def load_sessions_by_prefix(self, session_prefix: str) -> Iterator[ServerEvent]:
        """Yield events from every session sharing *session_prefix*, merged by timestamp."""
        ...

    @abstractmethod
    def list_sessions(self) -> set[str]:
        """Return the set of all full session ids that have at least one event."""
        ...

    @abstractmethod
    def list_sessions_by_prefix(self, session_prefix: str) -> set[str]:
        """Return the set of full session ids whose prefix matches *session_prefix*."""
        ...

    @abstractmethod
    def delete_session(self, session_id: str) -> None:
        """Remove all records for one full *session_id*."""
        ...

    @abstractmethod
    def delete_sessions_by_prefix(self, session_prefix: str) -> None:
        """Remove all records for every session matching *session_prefix*."""
        ...

    def last_updated(self, session_id: str) -> int | None:
        return None

    def load_materialized_by_prefix(self, session_prefix: str) -> list[MaterializedTurn]:
        """Materialize incremental events into merged turn blocks.

        Loads all events whose session ids share *session_prefix* and
        materializes them via :func:`_materialize_events`.
        Returns turns sorted by start time.
        """
        events: list[ServerEvent] = list(self.load_sessions_by_prefix(session_prefix))
        return _materialize_events(events)


class ResilientTranscriptStore(TranscriptStore):
    """Decorator that keeps the agent run alive when transcript I/O fails.

    ``append`` is on the hot path of every agent turn and tool call. A disk
    error (full disk, permission, transient I/O) must not crash the turn, so
    ``OSError`` from the delegate's ``append`` is logged and swallowed. Read /
    list / delete paths delegate unchanged — those run off the turn hot path
    (serving the UI) where surfacing errors is preferable to silent staleness.
    """

    def __init__(self, delegate: TranscriptStore) -> None:
        self._delegate = delegate

    def append(self, session_id: str, event: ServerEvent) -> None:
        try:
            self._delegate.append(session_id, event)
        except OSError:
            logger.exception(
                "transcript append failed for session %s (event=%s); "
                "continuing without persisting this event",
                session_id,
                getattr(event, "event", type(event).__name__),
            )

    def load(self, session_id: str) -> Iterator[ServerEvent]:
        return self._delegate.load(session_id)

    def load_sessions_by_prefix(self, session_prefix: str) -> Iterator[ServerEvent]:
        return self._delegate.load_sessions_by_prefix(session_prefix)

    def list_sessions(self) -> set[str]:
        return self._delegate.list_sessions()

    def list_sessions_by_prefix(self, session_prefix: str) -> set[str]:
        return self._delegate.list_sessions_by_prefix(session_prefix)

    def delete_session(self, session_id: str) -> None:
        self._delegate.delete_session(session_id)

    def delete_sessions_by_prefix(self, session_prefix: str) -> None:
        self._delegate.delete_sessions_by_prefix(session_prefix)

    def last_updated(self, session_id: str) -> int | None:
        return self._delegate.last_updated(session_id)


# ── Materialization helpers ────────────────────────────────────────────────


@_dataclass
class MaterializedTurn:
    """A complete ReAct turn materialized from incremental events.

    ``attachments`` carries serialized outbound :class:`Attachment` records
    collected from any :class:`AssistantTurnEvent` in this turn (populated by
    ``SendFileToUserTool``). Empty for turns that produced no files. An
    ``AssistantTurnEvent`` written with no ``turn_id`` (the standalone
    attachment-record carrier ``SendFileToUserTool`` persists) is emitted as
    its own ``MaterializedTurn`` with empty ``blocks`` and the record list, so
    the history-replay API returns it for the frontend to render download
    cards after a refresh (ADR-0013 §11).
    """
    turn_id: str = ""
    blocks: list[dict[str, object]] = _dc_field(default_factory=list)
    attachments: list[dict[str, object]] = _dc_field(default_factory=list)
    started_at: int = 0  # ms epoch


def _materialize_events(events: list[ServerEvent]) -> list[MaterializedTurn]:
    """Convert incremental transcript events into merged turn blocks.

    Groups events by turn_id, matches ToolCallEvent -> ToolResultEvent
    by call_id, and builds blocks arrays identical to the old
    AssistantTurnEvent.blocks format.

    Uses isinstance dispatch for type narrowing — this is a legitimate
    polymorphic boundary where events arrive as a heterogeneous list of
    ServerEvent subclasses (rule 6 + rule 9).
    """
    from bot.webui.events import (
        AssistantReasoningEvent,
        AssistantTextEvent,
        AssistantTurnEvent,
        ToolCallEvent,
        ToolResultEvent,
        TurnStartEvent,
    )

    _event_with_turn = (
        TurnStartEvent,
        AssistantReasoningEvent,
        AssistantTextEvent,
        ToolCallEvent,
        ToolResultEvent,
        AssistantTurnEvent,
    )
    turns: dict[str, list[ServerEvent]] = {}
    # AssistantTurnEvent carriers with NO turn_id — written by
    # ``SendFileToUserTool`` as standalone outbound-attachment records (no
    # conversational content, just the id→path index). They would be dropped
    # by the turn_id grouping below; preserve them as standalone turns so the
    # history-replay API returns them for rendering after refresh (ADR-0013 §11).
    standalone_attachment_turns: list[AssistantTurnEvent] = []
    for evt in events:
        if isinstance(evt, AssistantTurnEvent) and not evt.turn_id:
            standalone_attachment_turns.append(evt)
            continue
        if isinstance(evt, _event_with_turn) and evt.turn_id:
            turns.setdefault(evt.turn_id, []).append(evt)

    result: list[MaterializedTurn] = []

    for turn_id, group in turns.items():
        group_sorted = sorted(group, key=lambda e: e.timestamp)
        blocks: list[dict[str, object]] = []
        attachments: list[dict[str, object]] = []
        tool_calls: dict[str, dict[str, object]] = {}
        started_at: int = group_sorted[0].timestamp

        for evt in group_sorted:
            if isinstance(evt, AssistantTurnEvent):
                blocks.extend(evt.blocks)
                attachments.extend(evt.attachments)
            elif isinstance(evt, AssistantReasoningEvent):
                blocks.append({"kind": "reasoning", "text": evt.text})
            elif isinstance(evt, AssistantTextEvent):
                blocks.append({"kind": "text", "text": evt.text})
            elif isinstance(evt, ToolCallEvent):
                tool_calls[evt.call_id] = {
                    "tool": evt.tool_name,
                    "args": evt.args,
                }
            elif isinstance(evt, ToolResultEvent):
                entry = tool_calls.get(evt.call_id, {})
                block: dict[str, object] = {
                    "kind": "tool",
                    "tool": evt.tool_name,
                    "args": entry.get("args", {}),
                }
                if evt.error:
                    block["result"] = f"Error: {evt.error}"
                else:
                    block["result"] = evt.result
                blocks.append(block)

        result.append(MaterializedTurn(
            turn_id=turn_id,
            blocks=blocks,
            attachments=attachments,
            started_at=started_at,
        ))

    # Emit AssistantTurnEvent carriers with no turn_id as standalone turns,
    # preserving BOTH their blocks and attachments. Production
    # ``SendFileToUserTool`` writes these with blocks=[] (the record is the
    # only content), but preserving blocks too guards against any other
    # writer — never silently drop conversational content. Timestamp keeps
    # them in chronological order relative to the real turns.
    for evt in standalone_attachment_turns:
        result.append(MaterializedTurn(
            turn_id="",
            blocks=list(evt.blocks),
            attachments=list(evt.attachments),
            started_at=evt.timestamp,
        ))

    result.sort(key=lambda t: t.started_at)
    return result


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

    def load_sessions_by_prefix(self, session_prefix: str) -> Iterator[ServerEvent]:
        all_events: list[tuple[float, ServerEvent]] = []
        for session_id in self.list_sessions_by_prefix(session_prefix):
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

    def list_sessions_by_prefix(self, session_prefix: str) -> set[str]:
        prefix = safe_filename(session_prefix) + "."
        seen: set[str] = set()
        for f in self._iter_files():
            stem = self._session_id_of(f)
            if _session_id_prefix(stem) == safe_filename(session_prefix) or stem.startswith(prefix):
                seen.add(stem)
        return seen

    def delete_session(self, session_id: str) -> None:
        self._file_for(session_id).unlink(missing_ok=True)

    def delete_sessions_by_prefix(self, session_prefix: str) -> None:
        for session_id in self.list_sessions_by_prefix(session_prefix):
            self.delete_session(session_id)

    def last_updated(self, session_id: str) -> int | None:
        """Return transcript file mtime in milliseconds, or None if no file."""
        file_path = self._file_for(session_id)
        if not file_path.is_file():
            return None
        return int(file_path.stat().st_mtime * 1000)
