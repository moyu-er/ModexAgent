"""Unified termination model — Agent controlled-exit exceptions.

Hook, Interceptor, Control share the same termination semantics.
asyncio.CancelledError, KeyboardInterrupt, SystemExit must not be swallowed.
"""

from __future__ import annotations


class AgentControlError(Exception):
    """Controlled exit base exception.

    Represents controlled exit (not ordinary failure). All control-related
    exceptions should inherit from this class.
    """

    def __init__(self, reason: str = "") -> None:
        super().__init__(reason)


class AgentCancelled(AgentControlError):
    """External cancellation exception.

    Used when external control commands (e.g. user cancel, admin cancel)
    trigger Agent exit.
    """

    def __init__(self, reason: str = "Agent cancelled") -> None:
        super().__init__(reason)


class AgentTimeout(AgentControlError):
    """Timeout exception.

    Used for turn timeout, tool timeout, or overall run timeout.
    """

    def __init__(self, reason: str = "Agent timeout") -> None:
        super().__init__(reason)


class PolicyViolation(AgentControlError):
    """Policy violation exception.

    Used when pre-configured policies (e.g. token budget, safety policy)
    trigger termination.
    """

    def __init__(self, reason: str = "Policy violation") -> None:
        super().__init__(reason)
