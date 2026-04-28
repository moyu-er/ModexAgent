"""Control 核心类型。

定义 ControlCommand、ControlScope、ControlEvent、ControlCommandType、
ControlEventType 等控制平面基础类型。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class ControlCommandType(str, Enum):
    """控制命令类型枚举。"""

    CANCEL_RUN = "cancel_run"
    CANCEL_TURN = "cancel_turn"
    INJECT_USER_MESSAGE = "inject_user_message"
    APPROVAL_RESPONSE = "approval_response"
    SET_BUDGET_LIMIT = "set_budget_limit"
    CHECKPOINT_SAVE = "checkpoint_save"
    BACKGROUND_TOOL_RESULT = "background_tool_result"
    BACKGROUND_TOOL_PROGRESS = "background_tool_progress"
    PAUSE_RUN = "pause_run"
    RESUME_RUN = "resume_run"


class ControlEventType(str, Enum):
    """控制事件类型枚举。"""

    TOOL_APPROVAL_REQUESTED = "tool_approval_requested"
    TOOL_APPROVAL_RESOLVED = "tool_approval_resolved"
    BACKGROUND_TOOL_STARTED = "background_tool_started"
    BACKGROUND_TOOL_PROGRESS = "background_tool_progress"
    BACKGROUND_TOOL_COMPLETED = "background_tool_completed"
    RUN_CANCELLED = "run_cancelled"
    RUN_PAUSED = "run_paused"
    RUN_RESUMED = "run_resumed"
    TURN_TIMEOUT = "turn_timeout"
    CHECKPOINT_SAVED = "checkpoint_saved"


@dataclass(frozen=True)
class ControlScope:
    """控制命令/事件的作用域。"""

    session_id: str
    agent_id: str | None = None
    turn_id: str | None = None


@dataclass
class ControlCommand:
    """控制命令数据类。"""

    command_id: str
    type: ControlCommandType
    scope: ControlScope
    source: str = "external:user"
    priority: int = 0
    ttl_seconds: float | None = None
    correlation_id: str | None = None
    idempotency_key: str | None = None
    payload: dict[str, object] = field(default_factory=dict)


@dataclass
class ControlEvent:
    """控制事件数据类。"""

    event_id: str
    type: ControlEventType
    scope: ControlScope
    source: str = "system"
    correlation_id: str | None = None
    payload: dict[str, object] = field(default_factory=dict)


class ControlAction(str, Enum):
    """预设控制规则的响应动作。"""

    CANCEL_TURN = "cancel_turn"
    CANCEL_RUN = "cancel_run"
    NOTIFY = "notify"


@dataclass(frozen=True)
class ControlDecision:
    """控制命令处理后的决策。"""

    action: ControlAction = ControlAction.NOTIFY
    reason: str = ""
