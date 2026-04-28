"""内置 Interceptor 实现。

框架预置的常用拦截器：
- control_drain: ControlDrainInterceptor
- tool_approval: ToolApprovalInterceptor
- tool_timeout: ToolTimeoutInterceptor
- turn_timeout: TurnTimeoutInterceptor
- result_limit: ToolResultLimitInterceptor
"""

from framework.interceptor.builtin.control_drain import ControlDrainInterceptor
from framework.interceptor.builtin.result_limit import ToolResultLimitInterceptor
from framework.interceptor.builtin.tool_approval import (
    ApprovalDeniedAction,
    ApprovalTimeoutAction,
    ToolApprovalInterceptor,
    ToolNameMatcher,
)
from framework.interceptor.builtin.tool_timeout import ToolTimeoutInterceptor
from framework.interceptor.builtin.turn_timeout import (
    TimeoutAction,
    TurnTimeoutInterceptor,
)

__all__ = [
    "ApprovalDeniedAction",
    "ApprovalTimeoutAction",
    "ControlDrainInterceptor",
    "TimeoutAction",
    "ToolApprovalInterceptor",
    "ToolNameMatcher",
    "ToolResultLimitInterceptor",
    "ToolTimeoutInterceptor",
    "TurnTimeoutInterceptor",
]
