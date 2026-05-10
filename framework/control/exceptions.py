"""统一终止模型 —— Agent 受控退出异常。

Hook、Interceptor、Control 共享同一套终止语义。
asyncio.CancelledError、KeyboardInterrupt、SystemExit 不允许被吞掉。
"""

from __future__ import annotations

from enum import Enum


class TerminationReason(str, Enum):
    """受控终止原因枚举。"""

    CANCELLED = "cancelled"
    TIMEOUT = "timeout"
    APPROVAL_DENIED = "approval_denied"
    POLICY_VIOLATION = "policy_violation"
    MAX_ITERATIONS = "max_iterations"
    ERROR = "error"


class AgentControlError(Exception):
    """受控退出基类异常。

    表示受控退出（非普通失败）。所有控制相关异常应继承此类。
    """

    def __init__(self, reason: str = "", termination: TerminationReason | None = None) -> None:
        super().__init__(reason)
        self.termination = termination or TerminationReason.ERROR


class AgentCancelled(AgentControlError):
    """外部取消异常。

    用于外部控制命令（如用户取消、管理员取消）触发 Agent 退出。
    """

    def __init__(self, reason: str = "Agent cancelled") -> None:
        super().__init__(reason, termination=TerminationReason.CANCELLED)


class AgentTimeout(AgentControlError):
    """超时异常。

    用于 turn 超时、tool 超时或整体运行超时。
    """

    def __init__(self, reason: str = "Agent timeout") -> None:
        super().__init__(reason, termination=TerminationReason.TIMEOUT)


class ApprovalDenied(AgentControlError):
    """审批拒绝异常。

    用于工具调用审批被拒绝且策略为 CANCEL_TURN 时。
    """

    def __init__(self, reason: str = "Tool approval denied") -> None:
        super().__init__(reason, termination=TerminationReason.APPROVAL_DENIED)


class PolicyViolation(AgentControlError):
    """策略违反异常。

    用于预配置策略（如 token 预算、安全策略）触发终止时。
    """

    def __init__(self, reason: str = "Policy violation") -> None:
        super().__init__(reason, termination=TerminationReason.POLICY_VIOLATION)
