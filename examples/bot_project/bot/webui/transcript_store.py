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

ADR-0053/0054 transcript cutover: the durable record is the framework
``PresentationEvent`` plus the two bot-side carrier records — user messages
and outbound attachments — united in ``TranscriptRecord``. The persistence
lifecycle (append / load / list / delete / prefix merge) and the JSONL file
machinery are owned by the framework ``modex_agent.presentation`` transcript
contract; ``TranscriptRecordCodec`` detects the record generation per line so
pre-cutover ``ServerEvent`` transcripts keep replaying (the legacy read
adapter converts them at read time — the single conversion point). Turn
folding is the framework's ``materialize_turns``; ``materialize_records`` is
the history-API projection layered on top of it.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass as _dataclass
from dataclasses import field as _dc_field
from pathlib import Path
from typing import Annotated, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter

from bot.webui.events import (
    AssistantReasoningEvent,
    AssistantTextEvent,
    AssistantTurnEvent,
    ModelContentDelta,
    ModelReasoningDelta,
    ServerEvent,
    ToolCallEvent,
    ToolResultEvent,
    TurnStartEvent,
    UserMessageEvent,
)
from modex_agent.presentation import (
    JsonlTranscriptStore as FrameworkJsonlTranscriptStore,
)
from modex_agent.presentation import (
    PresentationEvent,
    PresentationEventBase,
    TextDelta,
    ThinkingDelta,
    ToolCallStarted,
    ToolResult,
    TranscriptCodec,
    TurnStarted,
    TurnView,
    materialize_turns,
)
from modex_agent.presentation import (
    TranscriptStore as FrameworkTranscriptStore,
)

logger = logging.getLogger(__name__)


# ── Durable record vocabulary (the post-cutover on-disk generation) ─────────


class UserMessageRecord(BaseModel):
    """A persisted user message (the S7 writer's record shape).

    ``attachments`` carries the serialized inbound :class:`Attachment`
    records (metadata only) produced by the attachment ingest stage — an
    open extension payload (``Attachment.to_dict()`` forms) whose keys
    evolve with the media layer.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["user_message"] = "user_message"
    session_id: str
    agent_name: str
    timestamp_ms: int = Field(default_factory=lambda: int(time.time() * 1000))
    content: str = ""
    attachments: list[dict[str, JsonValue]] = Field(default_factory=list)


class AttachmentCarrier(BaseModel):
    """Outbound attachment records riding the transcript (ADR-0013 §11).

    ``SendFileToUserTool`` persists one carrier per sent file. A carrier
    with an empty ``turn_id`` (the production shape) replays as its own
    turn so the history API keeps rendering download cards after a
    refresh; one carrying a turn id attaches its records to that turn —
    the replay shape of the pre-cutover ``assistant_turn`` records that
    carried attachments.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["attachments"] = "attachments"
    session_id: str
    agent_name: str
    timestamp_ms: int = Field(default_factory=lambda: int(time.time() * 1000))
    turn_id: str = ""
    attachments: list[dict[str, JsonValue]] = Field(default_factory=list)


TranscriptRecord = Annotated[
    UserMessageRecord | AttachmentCarrier | PresentationEvent,
    Field(discriminator="kind"),
]
"""The durable bot transcript record (discriminated by ``kind``).

The agent-turn facts are the framework's ``PresentationEvent`` union; the
two bot-side carriers cover what a UI conversation transcript needs that
the agent-turn envelope does not express (the user's side of the
conversation, and files the agent handed back).
"""


def _json_attachments(records: list[dict[str, object]]) -> list[dict[str, JsonValue]]:
    """Re-type serialized Attachment dicts at the JSON boundary."""
    return [cast(dict[str, JsonValue], dict(record)) for record in records]


# ── Legacy read adapter (pre-cutover ServerEvent lines) ─────────────────────


class ServerEventTranscriptCodec:
    """LEGACY READ adapter — decodes pre-cutover ``ServerEvent`` JSONL lines.

    This is not a write codec and never persists: since the transcript
    cutover no production path writes a ``ServerEvent`` to a store (the
    WebSocket wire projection is the only remaining ``ServerEvent``
    producer). The class survives exclusively so
    :class:`TranscriptRecordCodec` can read on-disk transcripts written
    before the cutover (``event``-discriminator lines), handing each
    decoded record to :func:`adapt_legacy_records` for read-time
    conversion. ``event_time`` keeps the legacy timestamp key so prefix
    merges order old and new records identically.
    """

    def parse(self, line: str) -> ServerEvent | None:
        """Decode one legacy JSONL line; ``None`` skips it (malformed/blank)."""
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            return None
        return ServerEvent.from_dict(data)

    def event_time(self, event: ServerEvent) -> int:
        return event.timestamp


def _legacy_segment_id() -> str:
    """A fresh segment id for one expanded legacy block.

    Each pre-materialized block IS one complete segment, so each must get
    its own id — reusing a stable id would let the folder merge two
    adjacent same-kind blocks that the legacy transcript kept apart.
    """
    return f"_legacy_{uuid.uuid4().hex[:8]}"


def earliest_user_content(records: Sequence[TranscriptRecord]) -> str | None:
    """The first persisted user message's raw content, ``None`` when absent.

    The session-title naming task's reader contract over loaded records:
    whitespace-only contents are skipped and the raw text is returned
    unstripped (the caller normalizes/truncates). Records arrive from
    ``TranscriptStore.load``, whose generation-detecting codec already
    adapts legacy files onto this vocabulary — matching the record class
    alone covers every on-disk generation.
    """
    for record in records:
        match record:
            case UserMessageRecord(content=content) if content.strip():
                return content
    return None


def adapt_legacy_records(event: ServerEvent) -> list[TranscriptRecord]:
    """Convert one legacy ``ServerEvent`` record to L2 transcript records.

    THE read-time conversion point: every legacy line decodes through the
    old registry and lands here, and replay never sees a ``ServerEvent``
    afterwards (no second materializer). Wire-only kinds that no writer
    ever persisted (tool_call_start/end, tool_args_delta, turn_end, ...)
    convert to nothing.
    """
    match event:
        case UserMessageEvent():
            return [
                UserMessageRecord(
                    session_id=event.session_id,
                    agent_name=event.agent_name,
                    timestamp_ms=event.timestamp,
                    content=event.content,
                    attachments=_json_attachments(event.attachments),
                )
            ]
        case ModelContentDelta():
            return [
                TextDelta(
                    session_id=event.session_id,
                    agent_name=event.agent_name,
                    turn_id=event.turn_id,
                    timestamp_ms=event.timestamp,
                    text=event.text,
                    segment_id=event.segment_id or "_text",
                )
            ]
        case ModelReasoningDelta():
            return [
                ThinkingDelta(
                    session_id=event.session_id,
                    agent_name=event.agent_name,
                    turn_id=event.turn_id,
                    timestamp_ms=event.timestamp,
                    text=event.text,
                    segment_id=event.segment_id or "_reasoning",
                )
            ]
        case TurnStartEvent():
            return [
                TurnStarted(
                    session_id=event.session_id,
                    agent_name=event.agent_name,
                    turn_id=event.turn_id,
                    timestamp_ms=event.timestamp,
                )
            ]
        case AssistantTextEvent():
            return [
                TextDelta(
                    session_id=event.session_id,
                    agent_name=event.agent_name,
                    turn_id=event.turn_id,
                    timestamp_ms=event.timestamp,
                    text=event.text,
                    segment_id=_legacy_segment_id(),
                )
            ]
        case AssistantReasoningEvent():
            return [
                ThinkingDelta(
                    session_id=event.session_id,
                    agent_name=event.agent_name,
                    turn_id=event.turn_id,
                    timestamp_ms=event.timestamp,
                    text=event.text,
                    segment_id=_legacy_segment_id(),
                )
            ]
        case ToolCallEvent():
            return [
                ToolCallStarted(
                    session_id=event.session_id,
                    agent_name=event.agent_name,
                    turn_id=event.turn_id,
                    timestamp_ms=event.timestamp,
                    tool_name=event.tool_name,
                    call_id=event.call_id or "",
                    arguments=cast(
                        dict[str, JsonValue], dict(event.args)
                    ),
                )
            ]
        case ToolResultEvent():
            return [
                ToolResult(
                    session_id=event.session_id,
                    agent_name=event.agent_name,
                    turn_id=event.turn_id,
                    timestamp_ms=event.timestamp,
                    tool_name=event.tool_name,
                    call_id=event.call_id or "",
                    output=event.result,
                    error=event.error,
                    seq=event.seq,
                )
            ]
        case AssistantTurnEvent():
            # Pre-materialized turn record: expand each block into its
            # presentation event (own segment — see _legacy_segment_id)
            # and ride the attachments on a carrier under the same turn.
            records: list[TranscriptRecord] = []
            for block in event.blocks:
                kind = str(block.get("kind", ""))
                text = block.get("text")
                if kind == "text":
                    records.append(
                        TextDelta(
                            session_id=event.session_id,
                            agent_name=event.agent_name,
                            turn_id=event.turn_id,
                            timestamp_ms=event.timestamp,
                            text=str(text or ""),
                            segment_id=_legacy_segment_id(),
                        )
                    )
                elif kind == "reasoning":
                    records.append(
                        ThinkingDelta(
                            session_id=event.session_id,
                            agent_name=event.agent_name,
                            turn_id=event.turn_id,
                            timestamp_ms=event.timestamp,
                            text=str(text or ""),
                            segment_id=_legacy_segment_id(),
                        )
                    )
                elif kind == "tool":
                    args = block.get("args")
                    # Started + result share one fresh call id so the
                    # folder pairs them back into a single tool card.
                    call_id = _legacy_segment_id()
                    records.append(
                        ToolCallStarted(
                            session_id=event.session_id,
                            agent_name=event.agent_name,
                            turn_id=event.turn_id,
                            timestamp_ms=event.timestamp,
                            tool_name=str(block.get("tool", "")),
                            call_id=call_id,
                            arguments=cast(
                                dict[str, JsonValue],
                                dict(args) if isinstance(args, dict) else {},
                            ),
                        )
                    )
                    records.append(
                        ToolResult(
                            session_id=event.session_id,
                            agent_name=event.agent_name,
                            turn_id=event.turn_id,
                            timestamp_ms=event.timestamp,
                            tool_name=str(block.get("tool", "")),
                            call_id=call_id,
                            output=str(block.get("result", "")),
                            error=cast(str | None, block.get("error")),
                        )
                    )
            if event.attachments:
                records.append(
                    AttachmentCarrier(
                        session_id=event.session_id,
                        agent_name=event.agent_name,
                        timestamp_ms=event.timestamp,
                        turn_id=event.turn_id,
                        attachments=_json_attachments(event.attachments),
                    )
                )
            return records
        case _:
            # Wire-only legacy kinds (never persisted by any writer) and
            # foreign records convert to nothing — same records today's
            # materializer dropped.
            return []


# ── Generation-detecting line codec ─────────────────────────────────────────


class TranscriptRecordCodec(TranscriptCodec[TranscriptRecord]):
    """Line codec for the durable ``TranscriptRecord`` generation.

    ``dump`` writes the new ``kind``-discriminator wire form. ``parse``
    detects the generation per line: a ``kind`` key decodes the record
    union directly; an ``event`` key is a legacy line, decoded by the
    legacy read adapter and converted at read time (a legacy
    ``assistant_turn`` line expands to several records).
    """

    _adapter: TypeAdapter[TranscriptRecord] = TypeAdapter(TranscriptRecord)
    _legacy: ServerEventTranscriptCodec = ServerEventTranscriptCodec()

    def dump(self, record: TranscriptRecord) -> str:
        # Loud contract check: a wrong-generation object (e.g. a wire
        # ``ServerEvent`` from before the record cutover) would otherwise
        # die five frames deep in ``model_dump_json`` — or worse, ride a
        # duck-typed path silently. The store accepts exactly the record
        # union; converters for other shapes live at their own seams.
        if not isinstance(record, UserMessageRecord | AttachmentCarrier | PresentationEventBase):
            raise TypeError(
                f"transcript records must be UserMessageRecord, "
                f"AttachmentCarrier, or a PresentationEvent — got "
                f"{type(record).__name__}; wire events never persist"
            )
        return record.model_dump_json()

    def parse(self, line: str) -> list[TranscriptRecord]:
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            return []
        if not isinstance(data, dict):
            return []
        if "kind" in data:
            try:
                return [self._adapter.validate_python(data)]
            except ValueError:
                return []
        legacy = self._legacy.parse(line)
        if legacy is None:
            return []
        return adapt_legacy_records(legacy)

    def event_time(self, record: TranscriptRecord) -> int:
        match record:
            case UserMessageRecord(timestamp_ms=ts) | AttachmentCarrier(
                timestamp_ms=ts
            ):
                return ts
            case _:
                return record.timestamp_ms if record.timestamp_ms is not None else 0


# ── Bot contract ───────────────────────────────────────────────────────────


class TranscriptStore(FrameworkTranscriptStore[TranscriptRecord], ABC):
    """Bot transcript contract: ``TranscriptRecord`` records + turn replay.

    The persistence lifecycle is the framework ABC's (ADR-0053); the bot
    adds the ``MaterializedTurn`` replay face consumed by the history
    APIs (folding is the framework's ``materialize_turns`` — see
    :func:`materialize_records`).
    """

    async def load_materialized_by_prefix(
        self,
        session_prefix: str,
        *,
        pool: str | None = None,
    ) -> list[MaterializedTurn]:
        """Replay every session sharing *session_prefix* into turn blocks."""
        events = await self.load_sessions_by_prefix(session_prefix, pool=pool)
        return materialize_records(events)


class WorkspaceRoutedTranscriptStore(TranscriptStore):
    """A transcript store that routes writes across workspaces.

    Extension boundary for the two store shapes the emitters consume:

    - A **workspace-routed** store (``WorkspaceScopedTranscriptStore``)
      multiplexes many workspace backends and accepts an optional
      ``sessions_dir=`` routing argument on ``append`` / the partial-buffer
      methods — the emitter passes its resolver-cell dir so the write lands
      in the owning workspace.
    - A **fixed** store (``JSONLTranscriptStore`` et al.) is already bound
      to one physical directory and takes no routing argument.

    Callers that hold a ``TranscriptStore`` and need to forward a workspace
    dir may ``isinstance``-check against this class — the one place the
    store-shape distinction is a real extension boundary (rule 6).
    """

    @abstractmethod
    async def append(
        self,
        session_id: str,
        event: TranscriptRecord,
        *,
        pool: str | None = None,
        sessions_dir: Path | None = None,
    ) -> None:
        """Persist a single event, optionally routed to *sessions_dir*'s
        workspace (``None`` = the store's own workspace resolution)."""
        ...

    @abstractmethod
    async def append_partial(
        self,
        session_id: str,
        event: TranscriptRecord,
        *,
        sessions_dir: Path | None = None,
    ) -> None:
        """Append a streaming delta to the in-memory partial buffer."""
        ...

    @abstractmethod
    async def clear_partial(
        self, session_id: str, sessions_dir: Path | None = None
    ) -> None:
        """Drop the in-memory partial buffer for *session_id*."""
        ...


class TranscriptPersistenceError(Exception):
    """A provider-specific persistence failure at the transcript seam."""


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

    async def append(
        self,
        session_id: str,
        event: TranscriptRecord,
        *,
        pool: str | None = None,
    ) -> None:
        try:
            await self._delegate.append(session_id, event, pool=pool)
        except (OSError, TranscriptPersistenceError):
            logger.exception(
                "transcript append failed for session %s (event=%s); "
                "continuing without persisting this event",
                session_id,
                event.kind,
            )

    async def load(self, session_id: str) -> list[TranscriptRecord]:
        return await self._delegate.load(session_id)

    async def load_sessions_by_prefix(
        self,
        session_prefix: str,
        *,
        pool: str | None = None,
    ) -> list[TranscriptRecord]:
        return await self._delegate.load_sessions_by_prefix(session_prefix, pool=pool)

    async def list_sessions(self) -> set[str]:
        return await self._delegate.list_sessions()

    async def list_sessions_by_prefix(self, session_prefix: str) -> set[str]:
        return await self._delegate.list_sessions_by_prefix(session_prefix)

    async def delete_session(self, session_id: str) -> None:
        await self._delegate.delete_session(session_id)

    async def delete_sessions_by_prefix(self, session_prefix: str) -> None:
        await self._delegate.delete_sessions_by_prefix(session_prefix)

    async def last_updated(self, session_id: str) -> int | None:
        return await self._delegate.last_updated(session_id)


# ── Replay projection (framework folder -> history-API shape) ───────────────


@_dataclass
class MaterializedTurn:
    """A complete turn materialized from transcript records.

    Folding is the framework's ``materialize_turns``;
    :func:`materialize_records` maps each ``TurnView`` onto this
    API-facing shape. ``attachments`` carries serialized outbound
    :class:`Attachment` records attached to the turn (a standalone
    carrier — the record ``SendFileToUserTool`` persists with no
    ``turn_id`` — replays as its own turn with empty ``blocks``, so the
    history-replay API returns it for the frontend to render download
    cards after a refresh, ADR-0013 §11).
    """
    turn_id: str = ""
    blocks: list[dict[str, object]] = _dc_field(default_factory=list)
    attachments: list[dict[str, object]] = _dc_field(default_factory=list)
    started_at: int = 0  # ms epoch


def _turn_view_blocks(view: TurnView) -> list[dict[str, object]]:
    """Map one framework ``TurnView`` onto the history-API block shape.

    The block dicts are the frontend contract (the same shape the
    pre-cutover ``AssistantTurnEvent.blocks`` carried): text / reasoning
    segments and tool cards with the error rendered into ``result``.
    """
    blocks: list[dict[str, object]] = []
    for block in view.blocks:
        if block.kind == "text":
            blocks.append({"kind": "text", "text": block.text})
        elif block.kind == "thinking":
            blocks.append({"kind": "reasoning", "text": block.text})
        else:
            result = (
                f"Error: {block.error}" if block.error is not None else block.output
            )
            blocks.append(
                {
                    "kind": "tool",
                    "tool": block.tool_name,
                    "args": dict(block.arguments) if block.arguments else {},
                    "result": result,
                }
            )
    return blocks


def materialize_records(records: Sequence[TranscriptRecord]) -> list[MaterializedTurn]:
    """Replay transcript records into the history-API materialized turns.

    The ONLY folding implementation is the framework's
    ``materialize_turns``; this projection maps each ``TurnView`` onto
    ``MaterializedTurn``, attaches ``AttachmentCarrier`` records by turn
    id (standalone carriers become their own turns), and derives each
    turn's start timestamp from its earliest record. Turns come back
    sorted by start time.

    The framework folder groups by CONTIGUITY, but the bot layer merges
    main + subagent session files by timestamp first — a main turn that
    dispatched a subagent mid-flight reaches the folder with its records
    interleaved with the subagent's, so its views must be merged back by
    turn id here (the composition is bot-owned; the framework's
    contiguity semantics stay untouched).
    """
    presentation: list[PresentationEvent] = []
    carriers: list[AttachmentCarrier] = []
    for record in records:
        if isinstance(record, UserMessageRecord):
            continue
        if isinstance(record, AttachmentCarrier):
            carriers.append(record)
            continue
        presentation.append(record)

    started_at: dict[str, int] = {}
    for event in presentation:
        timestamp = event.timestamp_ms if event.timestamp_ms is not None else 0
        turn_id = event.turn_id
        if turn_id not in started_at or timestamp < started_at[turn_id]:
            started_at[turn_id] = timestamp

    # Fold, then merge the fragments sharing one turn id: concatenate
    # blocks in fold (= record) order, which preserves each fragment's
    # internal order and the inter-fragment chronology. ``started_at``
    # already holds the turn's earliest record time.
    blocks_by_turn: dict[str, list[dict[str, object]]] = {}
    for view in materialize_turns(presentation):
        blocks_by_turn.setdefault(view.turn_id, []).extend(_turn_view_blocks(view))

    result: list[MaterializedTurn] = []
    for turn_id, blocks in blocks_by_turn.items():
        attachments: list[dict[str, JsonValue]] = []
        for carrier in carriers:
            if carrier.turn_id == turn_id:
                attachments.extend(carrier.attachments)
        # A turn with no blocks and no attachments has no replay surface —
        # observation-only groups (usage / approval records on an idle
        # identity) must not surface as empty assistant turns.
        if not blocks and not attachments:
            continue
        result.append(
            MaterializedTurn(
                turn_id=turn_id,
                blocks=blocks,
                attachments=attachments,
                started_at=started_at.get(turn_id, 0),
            )
        )

    # Carriers with no matching replayed turn (the standalone outbound
    # attachment records SendFileToUserTool persists) replay as their own
    # turns so the history API keeps rendering download cards after a
    # refresh — ONE turn PER carrier record, in record order, so two sent
    # files stay two cards at their own timestamps. Timestamp keeps them
    # chronological relative to real turns.
    for carrier in carriers:
        if carrier.turn_id in blocks_by_turn:
            continue
        result.append(
            MaterializedTurn(
                turn_id=carrier.turn_id,
                blocks=[],
                attachments=list(carrier.attachments),
                started_at=carrier.timestamp_ms,
            )
        )

    result.sort(key=lambda turn: turn.started_at)
    return result


# ── JSONL implementation (framework machinery + generation-detecting codec) ─


class JSONLTranscriptStore(FrameworkJsonlTranscriptStore[TranscriptRecord], TranscriptStore):
    """Stores events as one JSONL file per full session id.

    File layout: ``base_dir/{safe_session_id}.jsonl`` where *session_id* is the
    full receiver-owned identifier (``{conv}.{agent}[.{invocation_id}]``). The
    append/load/list/delete lifecycle is the framework JSONL store's
    (ADR-0053); the generation-detecting record codec and the replay face are
    bot-owned.
    """

    def __init__(self, base_dir: Path) -> None:
        super().__init__(base_dir, TranscriptRecordCodec())
