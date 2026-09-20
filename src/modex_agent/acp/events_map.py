"""Pure mappers: framework turn events / stop reasons -> ACP session updates.

This module is the test surface of the ACP adapter (ADR-0049): every function
is pure (no I/O, no connection state) and maps one framework concept to one
``acp.schema`` session-update model. ``acp.schema`` types are confined to the
mapping layer and ``server.py``/``entry.py`` — they must not leak further into
the framework.
"""

from __future__ import annotations

from acp.helpers import (
    start_tool_call,
    update_agent_message_text,
    update_agent_thought_text,
    update_tool_call,
    update_user_message_text,
)
from acp.schema import (
    AgentMessageChunk,
    AgentThoughtChunk,
    ToolCallProgress,
    ToolCallStart,
    ToolKind,
    UserMessageChunk,
)
from acp.schema import (
    StopReason as AcpStopReason,
)

from modex_agent.core.emitter import StopReason
from modex_agent.core.message import ChatMessage, MessageRole
from modex_agent.core.turn_events import (
    TurnEvent,
    TurnReasoningEvent,
    TurnTextEvent,
    TurnToolCallEvent,
    TurnToolResultEvent,
)

__all__ = [
    "SessionUpdateOut",
    "infer_tool_kind",
    "map_history_replay",
    "map_reasoning_event",
    "map_stop_reason",
    "map_text_event",
    "map_tool_call_event",
    "map_tool_result_event",
    "map_turn_event",
    "tool_call_id_for",
]

SessionUpdateOut = (
    AgentMessageChunk | AgentThoughtChunk | ToolCallStart | ToolCallProgress | UserMessageChunk
)
"""The session-update subtypes the event mappers can produce."""


def tool_call_id_for(turn_id: str, call_id: str) -> str:
    """Stable ACP toolCallId — turn-prefixed to avoid cross-turn collisions."""
    return f"{turn_id}:{call_id}"


# Tool-kind inference table (substring match on the lowercased tool name).
_READ_TOKENS = ("read", "glob", "grep")
_EDIT_TOKENS = ("write", "edit", "patch")
_EXECUTE_TOKENS = ("bash", "shell", "exec")
_FETCH_TOKENS = ("fetch",)
_THINK_TOKENS = ("think",)


def infer_tool_kind(tool_name: str) -> ToolKind:
    """Infer the ACP ``ToolKind`` hint from a framework tool name."""
    name = tool_name.lower()
    if any(token in name for token in _THINK_TOKENS):
        return "think"
    if any(token in name for token in _FETCH_TOKENS):
        return "fetch"
    if any(token in name for token in _EXECUTE_TOKENS):
        return "execute"
    if any(token in name for token in _EDIT_TOKENS):
        return "edit"
    if any(token in name for token in _READ_TOKENS):
        return "read"
    return "other"


def map_text_event(event: TurnTextEvent) -> AgentMessageChunk:
    """``TurnTextEvent`` -> ``agent_message_chunk``."""
    return update_agent_message_text(event.text)


def map_reasoning_event(event: TurnReasoningEvent) -> AgentThoughtChunk:
    """``TurnReasoningEvent`` -> ``agent_thought_chunk``."""
    return update_agent_thought_text(event.text)


def map_tool_call_event(event: TurnToolCallEvent, *, turn_id: str) -> ToolCallStart:
    """``TurnToolCallEvent`` -> ``tool_call`` start."""
    return start_tool_call(
        tool_call_id_for(turn_id, event.call_id),
        event.tool_name,
        kind=infer_tool_kind(event.tool_name),
        status="in_progress",
        raw_input=dict(event.arguments),
    )


def map_tool_result_event(
    event: TurnToolResultEvent, *, turn_id: str, failed: bool = False
) -> ToolCallProgress:
    """``TurnToolResultEvent`` -> ``tool_call_update`` (completed/failed + raw output)."""
    return update_tool_call(
        tool_call_id_for(turn_id, event.call_id),
        status="failed" if failed else "completed",
        raw_output=event.output,
    )


def map_turn_event(event: TurnEvent, *, turn_id: str) -> SessionUpdateOut:
    """Dispatch one framework turn event to its ACP session-update model."""
    if event.kind == "text":
        return map_text_event(event)
    if event.kind == "reasoning":
        return map_reasoning_event(event)
    if event.kind == "tool_call":
        return map_tool_call_event(event, turn_id=turn_id)
    return map_tool_result_event(event, turn_id=turn_id)


_STOP_REASON_MAP: dict[StopReason, AcpStopReason] = {
    StopReason.COMPLETED: "end_turn",
    StopReason.CANCELLED: "cancelled",
    StopReason.TURN_CANCELLED: "cancelled",
    StopReason.MAX_ITERATIONS: "max_turn_requests",
    StopReason.LOOP_DETECTED: "max_turn_requests",
    StopReason.ERROR: "refusal",
    StopReason.TIMEOUT: "refusal",
    StopReason.MISSED_COMMUNICATION: "refusal",
    StopReason.COMMAND_INTERCEPTED: "refusal",
    StopReason.DUPLICATE: "refusal",
}


def map_stop_reason(reason: StopReason) -> AcpStopReason:
    """Framework ``StopReason`` -> ACP stop-reason literal."""
    return _STOP_REASON_MAP[reason]


# Tool-call cards replayed from history are association-only (DESIGN.md §5.3):
# replay-scoped ids, never written back as framework facts.
_REPLAY_ID_PREFIX = "replay"


def _replay_text(content: str | list | None) -> str | None:
    """Text payload of a replayed message; non-text parts are never fabricated."""
    if isinstance(content, str):
        return content
    return None


def map_history_replay(messages: list[ChatMessage]) -> list[SessionUpdateOut]:
    """Project a typed history snapshot into replay updates, in true order.

    Only client-visible facts are replayed — user/assistant text and tool
    calls with their results. Missing reasoning, diffs, or results are not
    fabricated; non-client roles (system/tool bookkeeping without a matched
    call) are skipped.
    """
    updates: list[SessionUpdateOut] = []
    tool_ids: dict[str, str] = {}
    for index, message in enumerate(messages):
        if message.role is MessageRole.USER:
            text = _replay_text(message.content)
            if text is not None:
                updates.append(update_user_message_text(text))
            continue
        if message.role is MessageRole.ASSISTANT:
            text = _replay_text(message.content)
            if text is not None:
                updates.append(update_agent_message_text(text))
            for ordinal, call in enumerate(message.tool_calls or []):
                tool_id = f"{_REPLAY_ID_PREFIX}:{index}:{call.call_id if call.call_id is not None else ordinal}"
                if call.call_id is not None:
                    tool_ids[call.call_id] = tool_id
                updates.append(
                    start_tool_call(
                        tool_id,
                        call.tool_name,
                        kind=infer_tool_kind(call.tool_name),
                        status="in_progress",
                        raw_input=dict(call.arguments),
                    )
                )
            continue
        if message.role is MessageRole.TOOL and message.tool_call_id in tool_ids:
            text = _replay_text(message.content)
            updates.append(
                update_tool_call(
                    tool_ids[message.tool_call_id],
                    status="completed",
                    raw_output=text,
                )
            )
    return updates
