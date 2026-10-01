"""Transcript persistence contract + JSONL implementation (ADR-0053).

``TranscriptStore[E]`` is the generic persistence contract for per-session
event transcripts: append, load, list, delete. The record type ``E`` is
the extension point — the framework ships a presentation-event codec, and
a consumer that once persisted its own wire records decodes them in its
codec (adapting each legacy line to zero or more ``E`` records at read
time) while reusing the same JSONL machinery instead of re-deriving file
layout, prefix merging, and deletion semantics.

``materialize_turns`` folds a presentation-event stream back into
``TurnView``s (merged text/thinking segments, paired tool cards, terminal
stop reason) — the transcript round-trip contract.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter

from modex_agent.core.session_id import session_id_prefix_of
from modex_agent.core.turn_events import StopReason
from modex_agent.utils.file_io import safe_filename

from .events import (
    PresentationEvent,
    TextDelta,
    ThinkingDelta,
    ToolCallStarted,
    ToolResult,
    TurnFinished,
    TurnStarted,
)

E = TypeVar("E")
"""The persisted transcript record type (the consumer's wire model)."""


# ── Contract ───────────────────────────────────────────────────────────────


class TranscriptStore[E](ABC):
    """Abstract transcript store keyed by the full session id.

    The key is the receiver-owned identifier (``{prefix}.{agent}`` et al.)
    so two invocations of the same agent never collapse into one
    transcript; the session prefix (before the first ``.``) is the
    user-facing grouping.
    """

    @abstractmethod
    async def append(
        self,
        session_id: str,
        event: E,
        *,
        pool: str | None = None,
    ) -> None:
        """Persist a single event for *session_id* (full session identifier)."""
        ...

    @abstractmethod
    async def load(self, session_id: str) -> list[E]:
        """All events for *session_id*, oldest first."""
        ...

    @abstractmethod
    async def load_sessions_by_prefix(
        self,
        session_prefix: str,
        *,
        pool: str | None = None,
    ) -> list[E]:
        """Events from every session sharing *session_prefix*, merged in time order."""
        ...

    @abstractmethod
    async def list_sessions(self) -> set[str]:
        """All full session ids that have at least one event."""
        ...

    @abstractmethod
    async def list_sessions_by_prefix(self, session_prefix: str) -> set[str]:
        """Full session ids whose prefix matches *session_prefix*."""
        ...

    @abstractmethod
    async def delete_session(self, session_id: str) -> None:
        """Remove all records for one full *session_id*."""
        ...

    @abstractmethod
    async def delete_sessions_by_prefix(self, session_prefix: str) -> None:
        """Remove all records for every session matching *session_prefix*."""
        ...

    async def last_updated(self, session_id: str) -> int | None:
        """Last-update epoch ms for *session_id*, or ``None`` if unknown."""
        return None


class TranscriptCodec[E](ABC):
    """Line codec between a transcript record and its JSONL wire form.

    ``parse`` decodes one JSONL line into ZERO OR MORE records: a line of
    the codec's own generation yields one record, while a legacy line may
    expand into several records at read time (read-time generation
    adaptation) or be skipped when malformed/blank.
    """

    @abstractmethod
    def dump(self, event: E) -> str:
        """Serialize one record to a single JSONL line."""
        ...

    @abstractmethod
    def parse(self, line: str) -> list[E]:
        """Decode one JSONL line; empty list skips it (malformed/blank)."""
        ...

    def event_time(self, event: E) -> int:
        """Timestamp key for prefix merging (``0`` = unknown, sorts first)."""
        return 0


# ── JSONL implementation ───────────────────────────────────────────────────


class JsonlTranscriptStore[E](TranscriptStore[E]):
    """One JSONL file per full session id: ``{safe(session_id)}.jsonl``."""

    def __init__(self, base_dir: Path, codec: TranscriptCodec[E]) -> None:
        self._base_dir = base_dir
        self._codec = codec

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _file_for(self, session_id: str) -> Path:
        return self._base_dir / f"{safe_filename(session_id)}.jsonl"

    def _iter_files(self) -> list[Path]:
        if not self._base_dir.is_dir():
            return []
        return [
            file_path
            for file_path in self._base_dir.iterdir()
            if file_path.is_file() and file_path.suffix == ".jsonl"
        ]

    @staticmethod
    def _session_id_of(path: Path) -> str:
        """Reverse the safe-name mapping for a file stem.

        ``safe_filename`` only rewrites platform-unsafe characters; since
        session ids use ``.`` as their only separator, the stem is the
        session id.
        """
        return path.stem

    def _parse_line(self, line: str) -> list[E]:
        stripped = line.strip()
        if not stripped:
            return []
        return self._codec.parse(stripped)

    # ------------------------------------------------------------------
    # TranscriptStore interface
    # ------------------------------------------------------------------

    async def append(
        self,
        session_id: str,
        event: E,
        *,
        pool: str | None = None,
    ) -> None:
        del pool

        def _append() -> None:
            self._base_dir.mkdir(parents=True, exist_ok=True)
            line = self._codec.dump(event)
            with self._file_for(session_id).open("a", encoding="utf-8") as file:
                file.write(line + "\n")

        await asyncio.to_thread(_append)

    async def load(self, session_id: str) -> list[E]:
        def _load() -> list[E]:
            file_path = self._file_for(session_id)
            if not file_path.is_file():
                return []
            events: list[E] = []
            with file_path.open("r", encoding="utf-8") as file:
                for line in file:
                    events.extend(self._parse_line(line))
            return events

        return await asyncio.to_thread(_load)

    async def load_sessions_by_prefix(
        self,
        session_prefix: str,
        *,
        pool: str | None = None,
    ) -> list[E]:
        del pool
        merged: list[tuple[int, int, E]] = []
        sequence = 0
        for session_id in sorted(await self.list_sessions_by_prefix(session_prefix)):
            for event in await self.load(session_id):
                merged.append((self._codec.event_time(event), sequence, event))
                sequence += 1
        merged.sort(key=lambda entry: (entry[0], entry[1]))
        return [event for _, _, event in merged]

    async def list_sessions(self) -> set[str]:
        def _list() -> set[str]:
            return {self._session_id_of(f) for f in self._iter_files()}

        return await asyncio.to_thread(_list)

    async def list_sessions_by_prefix(self, session_prefix: str) -> set[str]:
        sessions = await self.list_sessions()
        safe_prefix = safe_filename(session_prefix)
        return {
            session_id
            for session_id in sessions
            if session_id_prefix_of(session_id) == safe_prefix
        }

    async def delete_session(self, session_id: str) -> None:
        await asyncio.to_thread(self._file_for(session_id).unlink, missing_ok=True)

    async def delete_sessions_by_prefix(self, session_prefix: str) -> None:
        for session_id in await self.list_sessions_by_prefix(session_prefix):
            await self.delete_session(session_id)

    async def last_updated(self, session_id: str) -> int | None:
        def _last_updated() -> int | None:
            file_path = self._file_for(session_id)
            if not file_path.is_file():
                return None
            return int(file_path.stat().st_mtime * 1000)

        return await asyncio.to_thread(_last_updated)


# ── Presentation-event codec + ready-to-use store ──────────────────────────


class PresentationTranscriptCodec(TranscriptCodec[PresentationEvent]):
    """``model_dump_json`` / discriminated-union ``validate_json`` codec."""

    _adapter: TypeAdapter[PresentationEvent] = TypeAdapter(PresentationEvent)

    def dump(self, event: PresentationEvent) -> str:
        return event.model_dump_json()

    def parse(self, line: str) -> list[PresentationEvent]:
        try:
            return [self._adapter.validate_json(line)]
        except ValueError:
            return []

    def event_time(self, event: PresentationEvent) -> int:
        return event.timestamp_ms if event.timestamp_ms is not None else 0


class PresentationTranscriptStore(JsonlTranscriptStore[PresentationEvent]):
    """JSONL transcript of presentation events with turn-view reads."""

    def __init__(self, base_dir: Path) -> None:
        super().__init__(base_dir, PresentationTranscriptCodec())

    async def load_turn_views(self, session_id: str) -> list[TurnView]:
        """Materialize the session's transcript into turn views."""
        return materialize_turns(await self.load(session_id))


# ── Turn-view materialization ──────────────────────────────────────────────


class TextBlock(BaseModel):
    """A merged text segment."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["text"] = "text"
    text: str


class ThinkingBlock(BaseModel):
    """A merged reasoning/thinking segment."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["thinking"] = "thinking"
    text: str


class ToolBlock(BaseModel):
    """One tool card: the call (full arguments) and its outcome."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["tool"] = "tool"
    tool_name: str
    call_id: str
    arguments: dict[str, JsonValue] | None = None
    output: str = ""
    error: str | None = None
    seq: int | None = None


TurnBlock = Annotated[
    TextBlock | ThinkingBlock | ToolBlock,
    Field(discriminator="kind"),
]


class TurnView(BaseModel):
    """A complete turn folded from its presentation events."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    turn_id: str
    stop_reason: StopReason | None = None
    error: str | None = None
    latency_ms: int | None = None
    blocks: list[TurnBlock] = Field(default_factory=list)


class _MutableToolBlock:
    """Accumulator for one tool card while folding a turn."""

    def __init__(self, tool_name: str, call_id: str) -> None:
        self.tool_name = tool_name
        self.call_id = call_id
        self.arguments: dict[str, JsonValue] | None = None
        self.output = ""
        self.error: str | None = None
        self.seq: int | None = None

    def freeze(self) -> ToolBlock:
        return ToolBlock(
            tool_name=self.tool_name,
            call_id=self.call_id,
            arguments=self.arguments,
            output=self.output,
            error=self.error,
            seq=self.seq,
        )


class _TextSlot:
    """Ordered slot for one contiguous text/thinking segment run."""

    def __init__(self, key: str, kind: Literal["text", "thinking"]) -> None:
        self.key = key
        self.kind = kind
        self.parts: list[str] = []


class _TurnAccumulator:
    """Folds one turn's presentation events into a ``TurnView``."""

    def __init__(self, turn_id: str) -> None:
        self.turn_id = turn_id
        self.tools: dict[str, _MutableToolBlock] = {}
        self.order: list[_TextSlot | _MutableToolBlock] = []
        self.stop_reason: StopReason | None = None
        self.error: str | None = None
        self.latency_ms: int | None = None

    def feed(self, event: PresentationEvent) -> None:
        match event:
            case TextDelta(text=text, segment_id=segment):
                self._text_slot("text", segment).parts.append(text)
            case ThinkingDelta(text=text, segment_id=segment):
                self._text_slot("thinking", segment).parts.append(text)
            case ToolCallStarted(tool_name=name, call_id=call_id, arguments=args):
                tool = self.tools.get(call_id)
                if tool is None:
                    tool = _MutableToolBlock(tool_name=name, call_id=call_id)
                    self.tools[call_id] = tool
                    self.order.append(tool)
                tool.arguments = args
            case ToolResult(
                tool_name=name,
                call_id=call_id,
                output=output,
                error=error,
                seq=seq,
                arguments=args,
            ):
                tool = self.tools.get(call_id)
                if tool is None:
                    # Result without a started card (e.g. resumed approval
                    # turn): the card materializes at the result.
                    tool = _MutableToolBlock(tool_name=name, call_id=call_id)
                    self.tools[call_id] = tool
                    self.order.append(tool)
                tool.output = output
                tool.error = error
                tool.seq = seq
                if args is not None:
                    tool.arguments = args
            case TurnFinished(
                stop_reason=stop_reason, error=error, latency_ms=latency
            ):
                self.stop_reason = stop_reason
                self.error = error
                self.latency_ms = latency

    def _text_slot(self, kind: Literal["text", "thinking"], segment: str) -> _TextSlot:
        """Return the slot continuing this segment's CURRENT run.

        A segment's deltas merge only while its slot is the newest item:
        once a tool card (or another segment) intervenes, the segment's
        next delta opens a NEW slot, so replay never hoists later text
        above the tool card that actually separated the runs.
        """
        head = self.order[-1] if self.order else None
        if isinstance(head, _TextSlot) and head.kind == kind and head.key == segment:
            return head
        slot = _TextSlot(key=segment, kind=kind)
        self.order.append(slot)
        return slot

    def view(self) -> TurnView:
        self._order_seq_tool_cards()
        blocks: list[TurnBlock] = []
        for item in self.order:
            if isinstance(item, _MutableToolBlock):
                blocks.append(item.freeze())
            elif item.kind == "text":
                blocks.append(TextBlock(text="".join(item.parts)))
            else:
                blocks.append(ThinkingBlock(text="".join(item.parts)))
        return TurnView(
            turn_id=self.turn_id,
            stop_reason=self.stop_reason,
            error=self.error,
            latency_ms=self.latency_ms,
            blocks=blocks,
        )

    def _order_seq_tool_cards(self) -> None:
        """Redistribute tool cards carrying a ``seq`` hint by that hint.

        Parallel tools COMPLETE in arbitrary order; ``seq`` is the
        runtime's model-order hint. Cards with a seq are stable-sorted by
        it and redistributed to the positions those cards occupy, while
        seq-less cards (a legacy writer's results) keep their arrival
        slot.
        """
        indexed: list[tuple[int, _MutableToolBlock]] = [
            (index, item)
            for index, item in enumerate(self.order)
            if isinstance(item, _MutableToolBlock) and item.seq is not None
        ]
        if len(indexed) < 2:
            return
        ordered = sorted((card for _, card in indexed), key=lambda card: card.seq)
        for (index, _), card in zip(indexed, ordered, strict=True):
            self.order[index] = card


def materialize_turns(events: Sequence[PresentationEvent]) -> list[TurnView]:
    """Fold a presentation-event stream into turn views.

    A new turn opens at a ``TurnStarted``, at a turn-identity change, or
    after a terminal ``TurnFinished``; an idle terminal pair (identity
    ``""``) still forms its own view so error surfaces are never dropped.
    """
    views: list[TurnView] = []
    group: list[PresentationEvent] = []
    open_id: str | None = None

    def flush() -> None:
        if not group:
            return
        accumulator = _TurnAccumulator(open_id or "")
        for event in group:
            accumulator.feed(event)
        views.append(accumulator.view())

    for event in events:
        turn_closed = bool(group) and isinstance(group[-1], TurnFinished)
        identity_changed = open_id is not None and event.turn_id != open_id
        announced = isinstance(event, TurnStarted)
        if group and (turn_closed or identity_changed or announced):
            flush()
            group = []
        if not group:
            open_id = event.turn_id
        group.append(event)
    flush()
    return views


__all__ = [
    "JsonlTranscriptStore",
    "PresentationTranscriptCodec",
    "PresentationTranscriptStore",
    "TextBlock",
    "ThinkingBlock",
    "ToolBlock",
    "TranscriptCodec",
    "TranscriptStore",
    "TurnView",
    "materialize_turns",
]
