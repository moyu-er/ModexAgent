"""Built-in Interceptor implementations.

Framework-provided interceptors:
- result_limit: ToolResultLimitInterceptor
- tool_approval: ArgumentMatcher (tool path classification helper)
"""

from modex_agent.interceptor.builtin.result_limit import ToolResultLimitInterceptor
from modex_agent.interceptor.builtin.tool_approval import ArgumentMatcher

__all__ = [
    "ArgumentMatcher",
    "ToolResultLimitInterceptor",
]
