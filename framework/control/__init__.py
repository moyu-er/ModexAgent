"""framework.control — 运行时控制平面。

提供：
- 统一终止异常（AgentControlError 等）
- ControlCommand / ControlScope 控制命令类型
- ControlChannel / ControlEventBus 命令和事件通道
- RuntimeStateStore 运行时状态持久化
- PresetControlRule 预配置策略
"""

from framework.control.channel import ControlChannel, InMemoryControlChannel
from framework.runtime.models import ApprovalDenialContext
from framework.runtime.store import JsonFileTurnStateStore, NoOpTurnStateStore, TurnStateStore
from framework.control.event_bus import (
    CallbackControlEventBus,
    ControlEventBus,
    ControlEventHandler,
    Subscription,
)
from framework.control.exceptions import (
    AgentCancelled,
    AgentControlError,
    AgentTimeout,
    ApprovalDenied,
    PolicyViolation,
    TerminationReason,
)
from framework.control.preset import PresetControlRule, TokenBudgetControlRule
from framework.control.task_supervision import (
    NoOpSupervisionPolicy,
    SupervisionAction,
    SupervisionResult,
    TaskSupervisionPolicy,
    TaskSupervisor,
    TimeoutSupervisionPolicy,
)
from framework.control.policy_registry import SupervisionPolicyRegistry, SupervisionPolicySpec
from framework.control.types import (
    ControlAction,
    ControlCommand,
    ControlCommandType,
    ControlDecision,
    ControlEvent,
    ControlEventType,
    ControlScope,
)

__all__ = [
    # 异常
    "AgentCancelled",
    "AgentControlError",
    "AgentTimeout",
    "ApprovalDenialContext",
    "ApprovalDenied",
    "PolicyViolation",
    "TerminationReason",
    # 类型
    "ControlAction",
    "ControlCommand",
    "ControlCommandType",
    "ControlDecision",
    "ControlEvent",
    "ControlEventType",
    "ControlScope",
    # 通道 / 总线 / 状态存储
    "CallbackControlEventBus",
    "ControlChannel",
    "ControlEventBus",
    "ControlEventHandler",
    "InMemoryControlChannel",
    "JsonFileTurnStateStore",
    "NoOpTurnStateStore",
    "TurnStateStore",
    "Subscription",
    # 预设规则
    "PresetControlRule",
    "TokenBudgetControlRule",
    # Task supervision
    "NoOpSupervisionPolicy",
    "SupervisionAction",
    "SupervisionResult",
    "SupervisionPolicyRegistry",
    "SupervisionPolicySpec",
    "TaskSupervisionPolicy",
    "TaskSupervisor",
    "TimeoutSupervisionPolicy",
]
