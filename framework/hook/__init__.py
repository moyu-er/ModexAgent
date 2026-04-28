"""framework.hook — Agent 生命周期扩展点。

提供：
- Hook 协议与 HookPoint 调度点枚举
- HookRunner 调度器
- 内置 Hook 实现（logging、runtime_context、inbox_flush、peer_auto_send、subagent_cleanup）
"""

from framework.hook.abc import (
    Hook,
    HookErrorPolicy,
    HookPayload,
    HookPoint,
    HookResult,
    HookSpec,
)
from framework.hook.runner import HookRunner

__all__ = [
    "Hook",
    "HookErrorPolicy",
    "HookPayload",
    "HookPoint",
    "HookResult",
    "HookRunner",
    "HookSpec",
]
