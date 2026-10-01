"""Built-in Interceptor implementations.

Framework-provided interceptors:
- tool_timeout: ToolTimeoutInterceptor (mandatory, composed by ToolExecutor)
- result_limit: ToolResultLimitInterceptor (in ``tools/overflow/`` — the
  overflow vertical owns its trigger side too)

``ArgumentMatcher`` moved to ``approval/argument_matcher.py`` (W2 — pure
approval classification helper, not an interceptor).
"""

from modex_agent.interceptor.builtin.tool_timeout import ToolTimeoutInterceptor

__all__ = [
    "ToolTimeoutInterceptor",
]
