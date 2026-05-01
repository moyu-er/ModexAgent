"""approval/response.py — parse_approval_action() 纯函数, 两套策略通用."""

from __future__ import annotations

from framework.approval.types import ApprovalAction

_APPROVE_ALIASES = frozenset(
    {
        "/approve",
        "approve",
        "/allow",
        "allow",
        "/yes",
        "yes",
        "/ok",
        "ok",
    }
)
_DENY_ALIASES = frozenset(
    {
        "/deny",
        "deny",
        "/reject",
        "reject",
        "/no",
        "no",
        "/cancel",
        "cancel",
    }
)


def parse_approval_action(text: str) -> ApprovalAction | None:
    """将用户文本解析为审批动作。纯函数，无副作用。

    剪枝: 输入超过 30 字符直接返回 None。
    匹配: 大小写不敏感, 去除前后空白。
    """
    if len(text) > 30:
        return None
    cmd = text.strip().lower()
    if cmd in _APPROVE_ALIASES:
        return ApprovalAction.ALLOW
    if cmd in _DENY_ALIASES:
        return ApprovalAction.DENY
    return None
