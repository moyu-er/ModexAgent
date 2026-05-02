"""framework.control — 运行时控制平面。

提供：
- 统一终止异常（AgentControlError 等）
- ControlCommand / ControlScope 控制命令类型
- ControlChannel / ControlEventBus 命令和事件通道
- CheckpointStore 检查点持久化
- PresetControlRule 预配置策略
"""

from framework.control.channel import ControlChannel, InMemoryControlChannel
from framework.control.checkpoint import (
    AgentCheckpoint,
    ApprovalDenialContext,
    CheckpointStore,
    JsonFileCheckpointStore,
    JsonFileRuntimeStateStore,
    NoOpCheckpointStore,
    NoOpRuntimeStateStore,
    RuntimeStateStore,
)
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
    "AgentCheckpoint",
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
    # 通道 / 总线 / 检查点
    "CallbackControlEventBus",
    "CheckpointStore",
    "ControlChannel",
    "ControlEventBus",
    "ControlEventHandler",
    "InMemoryControlChannel",
    "JsonFileCheckpointStore",
    "JsonFileRuntimeStateStore",
    "NoOpCheckpointStore",
    "NoOpRuntimeStateStore",
    "RuntimeStateStore",
    "Subscription",
    # 预设规则
    "PresetControlRule",
    "TokenBudgetControlRule",
]
