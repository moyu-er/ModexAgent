"""framework.control — runtime control plane.

Provides:
- Unified termination exceptions (AgentControlError etc.)
- ControlCommand / ControlScope control command types
- InMemoryControlChannel command input channel
- GraphControlService — external control for graph instances (ticket 10 §3.3)
"""

from modex_agent.control.channel import InMemoryControlChannel
from modex_agent.control.exceptions import (
    AgentCancelled,
    AgentControlError,
    AgentTimeout,
    PolicyViolation,
)
from modex_agent.control.graph_control import (
    GraphControlService,
    GraphEngineController,
    InMemoryGraphEngineController,
)
from modex_agent.control.graph_recovery import (
    GraphEngineFactory,
    GraphRecoveryService,
)
from modex_agent.control.types import (
    ControlCommand,
    ControlCommandType,
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
    "ControlScope",
    # Channel
    "InMemoryControlChannel",
    # Graph control (ticket 10 §3.3)
    "GraphControlService",
    "GraphEngineController",
    "InMemoryGraphEngineController",
    # Graph recovery (ticket 10 §3.5)
    "GraphEngineFactory",
    "GraphRecoveryService",
]
