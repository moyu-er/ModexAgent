"""framework.control — runtime control plane.

Provides:
- Unified termination exceptions (AgentControlError etc.)
- ControlCommand / ControlScope control command types
- InMemoryControlChannel command input channel
- GraphControlService — external control for graph instances
"""

from modex_agent.control.channel import InMemoryControlChannel
from modex_agent.control.exceptions import (
    AgentCancelledError,
    AgentControlError,
    AgentTimeoutError,
    PolicyViolationError,
)
from modex_agent.control.graph_control import (
    GraphControlService,
    GraphEngineController,
    InMemoryGraphEngineController,
)
from modex_agent.control.graph_recovery import (
    GraphRecoveryService,
)
from modex_agent.control.types import (
    ControlCommand,
    ControlCommandType,
    ControlScope,
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
    # Graph control
    "GraphControlService",
    "GraphEngineController",
    "InMemoryGraphEngineController",
    # Graph recovery
    "GraphRecoveryService",
]
