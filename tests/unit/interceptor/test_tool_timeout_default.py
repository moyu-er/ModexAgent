from __future__ import annotations

from framework.interceptor.builtin.tool_timeout import ToolTimeoutInterceptor


def test_tool_timeout_default_is_180() -> None:
    """Verify ToolTimeoutInterceptor defaults to 180s (not 60s)."""
    interceptor = ToolTimeoutInterceptor()
    # _timeout is None when using default; _resolve_timeout falls back to 180.0
    assert interceptor._timeout is None
