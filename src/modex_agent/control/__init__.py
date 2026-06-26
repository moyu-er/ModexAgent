"""framework.control — runtime control plane.

Provides:
- Unified termination exceptions (AgentControlError etc.)
- ControlCommand / ControlScope control command types
- InMemoryControlChannel command input channel
"""

from modex_agent.control.channel import InMemoryControlChannel
from modex_agent.control.exceptions import (
    AgentCancelled,
    AgentControlError,
    AgentTimeout,
    PolicyViolation,
)
from modex_agent.control.types import (
    ControlCommand,
    ControlCommandType,
    ControlEvent,
    ControlEventType,
    ControlScope,
)

__all__ = [
    # Exceptions
    "AgentCancelled",
    "AgentControlError",
    "AgentTimeout",
    "PolicyViolation",
    # Types
    "ControlCommand",
    "ControlCommandType",
    "ControlEvent",
    "ControlEventType",
    "ControlScope",
    # Channel
    "InMemoryControlChannel",
]
