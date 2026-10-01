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

ADR-0053 convergence: the persistence lifecycle (append / load / list /
delete / prefix merge) and the JSONL file machinery are owned by the
framework ``modex_agent.presentation`` transcript contract. The bot's
``TranscriptStore`` below is the ServerEvent-parameterized specialization
carrying the bot's block materialization face; ``JSONLTranscriptStore``
plugs a ``ServerEvent`` codec into the framework JSONL store so on-disk
bytes are unchanged.
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass as _dataclass
from dataclasses import field as _dc_field
from pathlib import Path

from bot.webui.events import ServerEvent
from modex_agent.presentation import (
    JsonlTranscriptStore as FrameworkJsonlTranscriptStore,
)
from modex_agent.presentation import (
    TranscriptCodec,
)
from modex_agent.presentation import (
    TranscriptStore as FrameworkTranscriptStore,
)

logger = logging.getLogger(__name__)


# ── ServerEvent codec (unchanged JSONL wire form) ──────────────────────────


class ServerEventTranscriptCodec(TranscriptCodec[ServerEvent]):
    """Serialize/deserialize ``ServerEvent`` records, byte-identical to the
    pre-convergence JSONL lines (``json.dumps(to_dict, ensure_ascii=False)``)."""

    def dump(self, event: ServerEvent) -> str:
        return json.dumps(event.to_dict(), ensure_ascii=False)

    def parse(self, line: str) -> ServerEvent | None:
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            return None
        return ServerEvent.from_dict(data)

    def event_time(self, event: ServerEvent) -> int:
        return event.timestamp


# ── Bot contract ───────────────────────────────────────────────────────────


class TranscriptStore(FrameworkTranscriptStore[ServerEvent], ABC):
    """Bot transcript contract: ``ServerEvent`` records + turn materialization.

    The persistence lifecycle is the framework ABC's (ADR-0053); the bot adds
    the ``MaterializedTurn`` blocks materialization consumed by the history
    replay API.
    """

    async def load_materialized_by_prefix(
        self,
        session_prefix: str,
        *,
        pool: str | None = None,
    ) -> list[MaterializedTurn]:
        """Materialize incremental events into merged turn blocks.

        Loads all events whose session ids share *session_prefix* and
        materializes them via :func:`_materialize_events`.
        Returns turns sorted by start time.
        """
        events = await self.load_sessions_by_prefix(session_prefix, pool=pool)
        return _materialize_events(events)


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
        event: ServerEvent,
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
        event: ServerEvent,
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
        event: ServerEvent,
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
                getattr(event, "event", type(event).__name__),
            )

    async def load(self, session_id: str) -> list[ServerEvent]:
        return await self._delegate.load(session_id)

    async def load_sessions_by_prefix(
        self,
        session_prefix: str,
        *,
        pool: str | None = None,
    ) -> list[ServerEvent]:
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
        tool_calls = {
            evt.call_id: {"tool": evt.tool_name, "args": evt.args}
            for evt in group_sorted
            if isinstance(evt, ToolCallEvent)
        }
        sequenced_tool_results = iter(
            evt
            for _seq, _position, evt in sorted(
                (evt.seq, position, evt)
                for position, evt in enumerate(group_sorted)
                if isinstance(evt, ToolResultEvent) and evt.seq is not None
            )
        )
        started_at: int = group_sorted[0].timestamp

        for evt in group_sorted:
            if isinstance(evt, AssistantTurnEvent):
                blocks.extend(evt.blocks)
                attachments.extend(evt.attachments)
            elif isinstance(evt, AssistantReasoningEvent):
                blocks.append({"kind": "reasoning", "text": evt.text})
            elif isinstance(evt, AssistantTextEvent):
                blocks.append({"kind": "text", "text": evt.text})
            elif isinstance(evt, ToolResultEvent):
                if evt.seq is not None:
                    evt = next(sequenced_tool_results)
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


# ── JSONL implementation (framework machinery + ServerEvent codec) ─────────


class JSONLTranscriptStore(FrameworkJsonlTranscriptStore[ServerEvent], TranscriptStore):
    """Stores events as one JSONL file per full session id.

    File layout: ``base_dir/{safe_session_id}.jsonl`` where *session_id* is the
    full receiver-owned identifier (``{conv}.{agent}[.{invocation_id}]``). The
    append/load/list/delete lifecycle is the framework JSONL store's
    (ADR-0053); the ``ServerEvent`` codec and the materialization face are
    bot-owned.
    """

    def __init__(self, base_dir: Path) -> None:
        super().__init__(base_dir, ServerEventTranscriptCodec())
