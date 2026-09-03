"""Unified termination model — Agent controlled-exit exceptions.

Hook, Interceptor, Control share the same termination semantics.
asyncio.CancelledError, KeyboardInterrupt, SystemExit must not be swallowed.
"""

from __future__ import annotations

from modex_agent.core.emitter import StopReason


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
