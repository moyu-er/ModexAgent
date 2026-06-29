"""Control core types.

Defines ControlCommand, ControlScope, ControlCommandType — the type definitions
for the control plane.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class ControlCommandType(str, Enum):
    """Control command types."""

    CANCEL_TURN = "cancel_turn"
    CANCEL_RUN = "cancel_run"
    INJECT_USER_MESSAGE = "inject_user_message"
    APPROVAL_RESPONSE = "approval_response"
    INJECT_STEER = "inject_steer"


@dataclass(frozen=True)
class ControlScope:
    """Scope for control commands/events."""

    session_id: str
    agent_id: str | None = None
    turn_id: str | None = None


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
