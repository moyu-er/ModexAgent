"""WebUI server-sent event dataclasses and protocol enums."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import ClassVar, get_origin, get_type_hints

# ── Protocol enums ────────────────────────────────────────────────────────


class WebUIEventType(str, Enum):
    """Discriminator for all WebUI server->client event types."""

    SERVER_EVENT = "server_event"
    USER_MESSAGE = "user_message"
    MODEL_CONTENT_DELTA = "model_content_delta"
    MODEL_REASONING_DELTA = "model_reasoning_delta"
    TOOL_CALL_START = "tool_call_start"
    TOOL_CALL_END = "tool_call_end"
    TURN_START = "turn_start"
    ASSISTANT_TEXT = "assistant_text"
    ASSISTANT_REASONING = "assistant_reasoning"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    TURN_END = "turn_end"
    ASSISTANT_TURN = "assistant_turn"
    CONVERSATION_READY = "conversation_ready"
    CONVERSATION_CREATED = "conversation_created"
    ATTACHED = "attached"
    CONVERSATION_DELETED = "conversation_deleted"
    ERROR = "error"


class WebSocketAction(str, Enum):
    """Client->server WebSocket action types."""

    ATTACH = "attach"
    SEND_MESSAGE = "send_message"
    DELETE_CONVERSATION = "delete_conversation"
    PAUSE = "pause"


# ── Helpers ────────────────────────────────────────────────────────────────


def _unwrap_envelope(data: dict[str, object]) -> dict[str, object]:
    """Convert a structured :class:`DeltaEnvelope` dict back to a flat
    ``ServerEvent`` dict (for tests and backward-compat consumers)."""
    if "event_type" not in data and "event" in data:
        return data  # already flat
    flat: dict[str, object] = {"event": data.get("event_type", "")}
    flat["session_id"] = data.get("session_id", "")
    flat["agent_name"] = data.get("agent_name", "")
    flat["timestamp"] = data.get("timestamp")
    payload = data.get("payload")
    if isinstance(payload, dict):
        flat.update(payload)
    return flat



# ── Legacy format migration ────────────────────────────────────────────────


def _migrate_assistant_turn(kwargs: dict[str, object]) -> dict[str, object]:
    """Convert old-format flat fields to ordered ``blocks`` list."""
    kwargs = dict(kwargs)
    blocks: list[dict[str, object]] = []
    reasoning = kwargs.pop("reasoning", None)
    if reasoning is not None:
        blocks.append({"kind": "reasoning", "text": str(reasoning)})
    content = kwargs.pop("content", None)
    if content is not None:
        blocks.append({"kind": "text", "text": str(content)})
    tools = kwargs.pop("tools", None)
    if isinstance(tools, list):
        for entry in tools:
            if isinstance(entry, dict):
                blocks.append({
                    "kind": "tool",
                    "tool": str(entry.get("tool", "")),
                    "args": entry.get("args", {}),
                    "result": entry.get("result", ""),
                })
    kwargs["blocks"] = blocks
    return kwargs


# ── Server->client event dataclasses ────────────────────────────────────────


@dataclass
class ServerEvent:
    """Base event for all WebUI server->client events.

    ``session_id`` is the **full session identifier**
    (``{conv_id}.{agent_name}``), matching the memory system.

    ``agent_name`` is the main agent within the pool that generated
    this event.

    ``timestamp`` is a millisecond-level Unix epoch integer.
    """

    session_id: str
    agent_name: str
    timestamp: int = field(default_factory=lambda: int(time.time() * 1000))

    event: str = field(default=WebUIEventType.SERVER_EVENT.value, init=False)

    _registry: ClassVar[dict[str, type[ServerEvent]]] = {}

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        event_field = cls.__dict__.get("event")
        try:
            ServerEvent._registry[event_field.default] = cls
        except AttributeError:
            pass

    def to_dict(self) -> dict[str, object]:
        hints = get_type_hints(type(self), include_extras=True)
        result: dict[str, object] = {}
        for field_name in self.__dataclass_fields__:
            hint = hints.get(field_name)
            if hint is not None and get_origin(hint) is ClassVar:
                continue
            val = getattr(self, field_name)
            if val is not None:
                result[field_name] = val
        return result

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> ServerEvent:
        event_type = str(data.get("event", ""))
        sub_cls = cls._registry.get(event_type, cls)
        kwargs = {k: v for k, v in data.items() if k != "event"}

        # Migrate old-format assistant_turn events
        if sub_cls is AssistantTurnEvent and "blocks" not in kwargs:
            kwargs = _migrate_assistant_turn(kwargs)

        # ── Field rename migration: session_id -> session_id ──
        if "session_id" not in kwargs and "session_id" in kwargs:
            cid = kwargs.pop("session_id")
            agent = kwargs.get("agent_name")
            # Old format: session_id was just the conv prefix.
            # Upgrade to full session_id.
            if isinstance(cid, str) and isinstance(agent, str) and "." not in cid:
                kwargs["session_id"] = f"{cid}.{agent}"
            else:
                kwargs["session_id"] = cid

        # ── Timestamp migration: float seconds -> int milliseconds ──
        ts = kwargs.get("timestamp")
        if isinstance(ts, float):
            kwargs["timestamp"] = int(ts * 1000)

        return sub_cls(**kwargs)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Concrete event types
# ---------------------------------------------------------------------------


@dataclass
class UserMessageEvent(ServerEvent):
    """A user message received from the WebUI."""
    content: str = ""
    event: str = field(default=WebUIEventType.USER_MESSAGE.value, init=False)


@dataclass
class ModelContentDelta(ServerEvent):
    """A chunk of model-generated content (streaming)."""
    text: str = ""
    turn_id: str = ""
    event: str = field(default=WebUIEventType.MODEL_CONTENT_DELTA.value, init=False)


@dataclass
class ModelReasoningDelta(ServerEvent):
    """A chunk of model reasoning (streaming, e.g. thinking blocks)."""
    text: str = ""
    turn_id: str = ""
    event: str = field(default=WebUIEventType.MODEL_REASONING_DELTA.value, init=False)


@dataclass
class ToolCallStartEvent(ServerEvent):
    """A tool call has started."""
    tool: str = ""
    args: dict[str, object] = field(default_factory=dict)
    turn_id: str = ""
    event: str = field(default=WebUIEventType.TOOL_CALL_START.value, init=False)


@dataclass
class ToolCallEndEvent(ServerEvent):
    """A tool call has completed."""
    tool: str = ""
    result_summary: str = ""
    turn_id: str = ""
    event: str = field(default=WebUIEventType.TOOL_CALL_END.value, init=False)


@dataclass
class TurnEndEvent(ServerEvent):
    """A turn (agent invocation) has ended (streaming notification, not persisted)."""
    turn_id: str = ""
    latency_ms: float = 0.0
    event: str = field(default=WebUIEventType.TURN_END.value, init=False)


@dataclass
class TurnStartEvent(ServerEvent):
    """Emitted when a new ReAct turn begins (lazy, on first content event)."""
    turn_id: str = ""
    event: str = field(default=WebUIEventType.TURN_START.value, init=False)


@dataclass
class AssistantTextEvent(ServerEvent):
    """LLM text output for one round — clean content, no reasoning."""
    turn_id: str = ""
    text: str = ""
    event: str = field(default=WebUIEventType.ASSISTANT_TEXT.value, init=False)


@dataclass
class AssistantReasoningEvent(ServerEvent):
    """LLM reasoning/thinking content persisted as an independent event."""
    turn_id: str = ""
    text: str = ""
    event: str = field(default=WebUIEventType.ASSISTANT_REASONING.value, init=False)


@dataclass
class ToolCallEvent(ServerEvent):
    """Tool call parameters from assistant. Linked to ToolResultEvent by call_id."""
    turn_id: str = ""
    call_id: str = ""
    tool_name: str = ""
    args: dict[str, object] = field(default_factory=dict)
    event: str = field(default=WebUIEventType.TOOL_CALL.value, init=False)


@dataclass
class ToolResultEvent(ServerEvent):
    """Tool execution result. Linked to ToolCallEvent by call_id."""
    turn_id: str = ""
    call_id: str = ""
    tool_name: str = ""
    result: str = ""
    error: str | None = None
    event: str = field(default=WebUIEventType.TOOL_RESULT.value, init=False)


@dataclass
class AssistantTurnEvent(ServerEvent):
    """Complete assistant turn — persisted to transcript store at turn end."""
    blocks: list[dict[str, object]] = field(default_factory=list)
    turn_id: str = ""
    latency_ms: float = 0.0
    event: str = field(default=WebUIEventType.ASSISTANT_TURN.value, init=False)


@dataclass
class ConversationCreatedEvent(ServerEvent):
    """A new subagent conversation was spawned under its parent session."""
    parent_session_id: str | None = None
    event: str = field(default=WebUIEventType.CONVERSATION_CREATED.value, init=False)


# ---------------------------------------------------------------------------
# Structured transport envelope
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionMeta:
    """Business routing context for a session, resolved at send time.

    ``pool`` is the authoritative pool the session's agent belongs to (from
    the configured agent→pool map — deterministic, not inferred).

    ``parent_session_id`` is the session that dispatched this one, sourced from
    the dispatch-layer runtime parent registry.  ``None`` for main-agent
    sessions or while the registry is not yet populated.
    """

    pool: str = ""
    parent_session_id: str | None = None


# Fields that belong on the envelope (routing/context), not in the payload.
_ENVELOPE_KEYS: frozenset[str] = frozenset(
    {"session_id", "agent_name", "event", "timestamp"}
)


@dataclass
class DeltaEnvelope:
    """Structured container for one server→client WebSocket message.

    The envelope separates routing/context (``session_id``, ``agent_name``,
    ``pool``, ``event_type``, ``timestamp``) from a free-form, extensible
    ``metadata`` dict and the event-specific ``payload``.  Transporting the
    structured object (not a flat JSON string) keeps the wire format
    extensible: new context lands in ``metadata`` without touching every event
    type.

    ``parent_session_id`` carries the session that *dispatched* this one — set
    only for subagent sessions, sourced from the dispatch-layer runtime parent
    registry (NOT inferred from the session id).  ``None`` for main-agent
    sessions or when the parent is unknown.

    Serialized once at the WebSocket boundary via :meth:`to_dict`.
    """

    session_id: str
    agent_name: str
    event_type: str
    pool: str = ""
    parent_session_id: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)
    payload: dict[str, object] = field(default_factory=dict)
    timestamp: int = field(default_factory=lambda: int(time.time() * 1000))

    @classmethod
    def from_event(
        cls,
        event: ServerEvent,
        metadata: dict[str, object] | None = None,
        *,
        pool: str = "",
        parent_session_id: str | None = None,
    ) -> DeltaEnvelope:
        """Build an envelope from a :class:`ServerEvent`.

        Routing fields (session_id, agent_name, event type, timestamp) move to
        the envelope; all event-specific fields become the ``payload``.
        ``pool``/``parent_session_id`` carry business routing context.
        """
        data: dict[str, object] = event.to_dict()
        event_type = str(data.pop("event", ""))
        session_id = str(data.pop("session_id", ""))
        agent_name = str(data.pop("agent_name", ""))
        ts_raw = data.pop("timestamp", None)
        timestamp = int(ts_raw) if isinstance(ts_raw, (int, float)) else int(time.time() * 1000)
        return cls(
            session_id=session_id,
            agent_name=agent_name,
            event_type=event_type,
            pool=pool,
            parent_session_id=parent_session_id,
            metadata=dict(metadata) if metadata else {},
            payload=data,
            timestamp=timestamp,
        )

    @classmethod
    def content(
        cls,
        *,
        session_id: str,
        agent_name: str,
        text: str,
        metadata: dict[str, object] | None = None,
        pool: str = "",
        parent_session_id: str | None = None,
    ) -> DeltaEnvelope:
        """Wrap a plain content string (framework ``send_delta`` fallback)."""
        return cls(
            session_id=session_id,
            agent_name=agent_name,
            event_type="content",
            pool=pool,
            parent_session_id=parent_session_id,
            metadata=dict(metadata) if metadata else {},
            payload={"text": text},
        )

    def to_dict(self) -> dict[str, object]:
        """Serialize for the wire (called once at the WebSocket boundary)."""
        return {
            "session_id": self.session_id,
            "agent_name": self.agent_name,
            "pool": self.pool,
            "parent_session_id": self.parent_session_id,
            "event_type": self.event_type,
            "timestamp": self.timestamp,
            "metadata": self.metadata,
            "payload": self.payload,
        }

    def to_event(self) -> ServerEvent:
        """Reconstruct the source :class:`ServerEvent` from this envelope."""
        data: dict[str, object] = {
            "event": self.event_type,
            "session_id": self.session_id,
            "agent_name": self.agent_name,
            "timestamp": self.timestamp,
        }
        data.update(self.payload)
        return ServerEvent.from_dict(data)
