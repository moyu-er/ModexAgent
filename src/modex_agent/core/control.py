"""Control-plane vocabulary — command types, scopes, and termination exceptions.

Sank to core (W3b) from ``control/types.py`` + ``control/exceptions.py``:
Hook, Interceptor, and Commands (all level 1) share these types with the
runtime control plane, so the shared vocabulary lives below all of them.
The in-memory channel implementation stays in
:mod:`modex_agent.control.channel`.

Unified termination model: Hook, Interceptor, Control share the same
termination semantics. ``asyncio.CancelledError``, ``KeyboardInterrupt``,
``SystemExit`` must not be swallowed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from modex_agent.core.emitter import StopReason

__all__ = [
    "AgentCancelledError",
    "AgentControlError",
    "AgentTimeoutError",
    "ControlCommand",
    "ControlCommandType",
    "ControlScope",
    "LoopDetectedError",
    "PolicyViolationError",
]


# ── Command vocabulary ────────────────────────────────────────────────

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


# ── Controlled-exit exceptions ─────────────────────────────────────────

class AgentControlError(Exception):
    """Controlled exit base exception.

    Represents controlled exit (not ordinary failure). All control-related
    exceptions should inherit from this class.
    """

    user_content: str = ""
    stop_reason: StopReason = StopReason.CANCELLED

    def __init__(self, reason: str = "") -> None:
        super().__init__(reason)


class AgentCancelledError(AgentControlError):
    """External cancellation exception.

    Used when external control commands (e.g. user cancel, admin cancel)
    trigger Agent exit.
    """

    stop_reason: StopReason = StopReason.CANCELLED

    def __init__(self, reason: str = "Agent cancelled") -> None:
        super().__init__(reason)


class AgentTimeoutError(AgentControlError):
    """Timeout exception.

    Used for turn timeout, tool timeout, or overall run timeout.
    """

    stop_reason: StopReason = StopReason.TIMEOUT

    def __init__(self, reason: str = "Agent timeout") -> None:
        super().__init__(reason)


class PolicyViolationError(AgentControlError):
    """Policy violation exception.

    Used when pre-configured policies (e.g. token budget, safety policy)
    trigger termination.
    """

    stop_reason: StopReason = StopReason.ERROR

    def __init__(self, reason: str = "Policy violation") -> None:
        super().__init__(reason)


class LoopDetectedError(AgentControlError):
    """ReAct loop detected — force end of current turn."""

    stop_reason: StopReason = StopReason.LOOP_DETECTED

    def __init__(self, user_content: str, loop_type: str) -> None:
        super().__init__(f"Loop detected ({loop_type})")
        self.user_content = user_content
        self.loop_type = loop_type
