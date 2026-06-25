from __future__ import annotations

from enum import Enum


class AgentState(Enum):
    """常驻 Agent 的运行时状态机。"""

    INITIALIZING = "initializing"
    IDLE = "idle"
    WORKING = "working"
    ERROR = "error"
    SHUTTING_DOWN = "shutting_down"
    SHUTDOWN = "shutdown"
