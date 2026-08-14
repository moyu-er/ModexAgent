"""Control core types.

Defines ControlCommand, ControlScope, ControlCommandType — the type definitions
for the control plane.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class ControlCommandType(StrEnum):
    """Control command types."""

    CANCEL_TURN = "cancel_turn"
    CANCEL_RUN = "cancel_run"
    INJECT_USER_MESSAGE = "inject_user_message"
    APPROVAL_RESPONSE = "approval_response"
    INJECT_STEER = "inject_steer"
    # Graph instance lifecycle control.
    PAUSE_GRAPH = "pause_graph"
    STOP_GRAPH = "stop_graph"
    RESUME_GRAPH = "resume_graph"
    DELIVER_TO_NODE = "deliver_to_node"


@dataclass(frozen=True)
class ControlScope:
    """Scope for control commands/events.

    For graph-scoped commands (PAUSE_GRAPH / STOP_GRAPH / RESUME_GRAPH /
    DELIVER_TO_NODE), `graph_instance_id` identifies the target
    `GraphInstance`. The existing session_id/agent_id/turn_id fields stay
    — a graph instance lives within a session.
    """

    session_id: str
    agent_id: str | None = None
    turn_id: str | None = None
    graph_instance_id: int | None = None


@dataclass
class ControlCommand:
    """Control command data class."""

    command_id: str
    type: ControlCommandType
    scope: ControlScope
    source: str = "external:user"
    priority: int = 0
    ttl_seconds: float | None = None
    correlation_id: str | None = None
    idempotency_key: str | None = None
    payload: dict[str, object] = field(default_factory=dict)
