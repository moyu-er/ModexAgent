"""ApprovalRenderer — suspend → render → resume 审批流模块。

当前为 Phase 5 的第一步: 提取审批格式化等纯函数。完整的 detect/handle/drain
提取将在后续迭代中完成 (pipeline.py 中相关方法对 self 状态依赖过深)。
"""

from __future__ import annotations

from typing import Any


def format_approval_prompt(req: Any) -> str:
    """Format an approval request for display to the user."""
    tool_name = getattr(req, "tool_name", "unknown")
    call_id = getattr(req, "tool_call_id", "")
    args = getattr(req, "arguments", {})
    tier = getattr(req, "tier", "unknown")
    args_str = ", ".join(f"{k}={v}" for k, v in (args or {}).items())
    return (
        f"Approval Required [{tier.upper()}]\n"
        f"Tool: {tool_name}\n"
        f"ID: {call_id}\n"
        f"Args: {args_str}\n"
        f"Reply /approve or /deny"
    )
