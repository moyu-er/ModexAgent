"""framework.control — runtime control plane.

The shared control vocabulary (``ControlCommand`` / ``ControlScope`` /
``ControlCommandType``) and the unified termination exceptions
(``AgentControlError`` family) sank to :mod:`modex_agent.core.control`
(W3b) — Hook / Interceptor / Commands share them at level 1. This package
keeps the in-memory command channel implementation.

Graph control/recovery moved to ``orchestration`` (W2) — the graph
lifecycle services belong to the orchestration vertical.
"""

from modex_agent.control.channel import InMemoryControlChannel
from modex_agent.core.control import (
    AgentCancelledError,
    AgentControlError,
    AgentTimeoutError,
    ControlCommand,
    ControlCommandType,
    ControlScope,
    PolicyViolationError,
)

__all__ = [
    # Exceptions
    "AgentCancelledError",
    "AgentControlError",
    "AgentTimeoutError",
    "PolicyViolationError",
    # Types
    "ControlCommand",
    "ControlCommandType",
    "ControlScope",
    # Channel
    "InMemoryControlChannel",
]
