"""WebUI server-sent event dataclasses and protocol enums."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import ClassVar, get_origin, get_type_hints


# ── Protocol enums ────────────────────────────────────────────────────────


class WebUIEventType(str, Enum):
    """Discriminator for all WebUI server→client event types."""

    SERVER_EVENT = "server_event"
    USER_MESSAGE = "user_message"
    MODEL_CONTENT_DELTA = "model_content_delta"
    MODEL_REASONING_DELTA = "model_reasoning_delta"
    TOOL_CALL_START = "tool_call_start"
    TOOL_CALL_END = "tool_call_end"
    TURN_END = "turn_end"
    ASSISTANT_TURN = "assistant_turn"
    CONVERSATION_READY = "conversation_ready"
    ATTACHED = "attached"
    CONVERSATION_DELETED = "conversation_deleted"
    ERROR = "error"


class WebSocketAction(str, Enum):
    """Client→server WebSocket action types."""

    ATTACH = "attach"
    SEND_MESSAGE = "send_message"
    NEW_CONVERSATION = "new_conversation"
    DELETE_CONVERSATION = "delete_conversation"


# ── WebSocket client message dataclasses ───────────────────────────────────


@dataclass
class _WSClientMessage:
    """Base for typed WebSocket client messages.

    Subclasses declare the *action* discriminator via
    ``field(default=..., init=False)``.
    """

    action: str = field(default="", init=False)

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> _WSClientMessage:
        action = str(data.get("action", ""))
        sub_cls = _WS_MESSAGE_REGISTRY.get(action, cls)
        kwargs = {k: v for k, v in data.items() if k != "action"}
        return sub_cls(**kwargs)  # type: ignore[call-arg]


_WS_MESSAGE_REGISTRY: dict[str, type[_WSClientMessage]] = {}


def _register_ws_message(cls: type[_WSClientMessage]) -> None:
    field_obj = cls.__dict__.get("action")
    try:
        _WS_MESSAGE_REGISTRY[field_obj.default] = cls
    except AttributeError:
        pass


@dataclass
class AttachMessage(_WSClientMessage):
    """Client requests to attach to a conversation."""

    conversation_id: str = ""

    action: str = field(default=WebSocketAction.ATTACH.value, init=False)


@dataclass
class SendMessageMessage(_WSClientMessage):
    """Client sends a chat message to the main agent."""

    conversation_id: str = ""
    content: str = ""

    action: str = field(default=WebSocketAction.SEND_MESSAGE.value, init=False)


@dataclass
class NewConversationMessage(_WSClientMessage):
    """Client requests a new conversation."""

    action: str = field(default=WebSocketAction.NEW_CONVERSATION.value, init=False)


@dataclass
class DeleteConversationMessage(_WSClientMessage):
    """Client requests deletion of a conversation."""

    conversation_id: str = ""

    action: str = field(default=WebSocketAction.DELETE_CONVERSATION.value, init=False)


_register_ws_message(AttachMessage)
_register_ws_message(SendMessageMessage)
_register_ws_message(NewConversationMessage)
_register_ws_message(DeleteConversationMessage)


# ── Legacy format migration ────────────────────────────────────────────────


def _migrate_assistant_turn(
    kwargs: dict[str, object],
) -> dict[str, object]:
    """Convert old-format ``content``/``reasoning``/``tools`` to ``blocks`` list.

    Old transcript entries stored assistant turns as flat fields::

        {content: "...", reasoning: "...", tools: [{tool, args, result}, ...]}

    The current format uses an ordered ``blocks`` list preserving streaming
    interleaving.  The best-effort migration order is reasoning → content → tools.
    """
    kwargs = dict(kwargs)  # copy to avoid mutating caller
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


# ── Server→client event dataclasses ────────────────────────────────────────


@dataclass
class ServerEvent:
    """Base event for all WebUI server→client events.

    Subclasses override ``event`` via ``field(default=..., init=False)``
    with a :class:`WebUIEventType` value.
    """

    conversation_id: str
    agent_name: str
    timestamp: float = field(default_factory=time.time)

    # Discriminator — each subclass sets its own constant string.
    event: str = field(default=WebUIEventType.SERVER_EVENT.value, init=False)

    # Registry: event string → subclass, populated on import.
    _registry: ClassVar[dict[str, type[ServerEvent]]] = {}

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        event_field = cls.__dict__.get("event")
        try:
            ServerEvent._registry[event_field.default] = cls
        except AttributeError:
            pass

    def to_dict(self) -> dict[str, object]:
        """Serialize to dict, excluding None values and ClassVar fields."""
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
        """Factory: dispatch on the ``event`` field to the correct subclass."""
        event_type = str(data.get("event", ""))
        sub_cls = cls._registry.get(event_type, cls)
        kwargs = {k: v for k, v in data.items() if k != "event"}

        # Migrate old-format assistant_turn events (content/reasoning/tools → blocks).
        # Old format stored flat fields; new format uses ordered blocks list.
        if sub_cls is AssistantTurnEvent and "blocks" not in kwargs:
            kwargs = _migrate_assistant_turn(kwargs)

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
class AssistantTurnEvent(ServerEvent):
    """Complete assistant turn — persisted to transcript store at turn end.

    ``blocks`` preserves the exact interleaving order of content, reasoning,
    and tool calls from the streaming phase.  Not sent to WebSocket clients.
    """

    blocks: list[dict[str, object]] = field(default_factory=list)
    turn_id: str = ""
    latency_ms: float = 0.0

    event: str = field(default=WebUIEventType.ASSISTANT_TURN.value, init=False)
