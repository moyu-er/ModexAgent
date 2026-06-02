"""Built-in Interceptor implementations.

Framework-provided interceptors:
- control_drain: ControlDrainInterceptor
- result_limit: ToolResultLimitInterceptor
- tool_approval: ArgumentMatcher (tool path classification helper)
"""

from framework.interceptor.builtin.control_drain import ControlDrainInterceptor
from framework.interceptor.builtin.result_limit import ToolResultLimitInterceptor
from framework.interceptor.builtin.tool_approval import ArgumentMatcher

__all__ = [
    "ArgumentMatcher",
    "ControlDrainInterceptor",
    "ToolResultLimitInterceptor",
]
