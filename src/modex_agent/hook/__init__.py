"""framework.hook — Agent 生命周期扩展点。

提供：
- Hook 协议与 HookPoint 调度点枚举
- HookRunner 调度器
- 内置 Hook 实现（logging、runtime_context、inbox_flush、subagent_auto_send、subagent_cleanup）
"""

from modex_agent.hook.abc import (
    AfterIterationHook,
    AfterLLMResponseHook,
    AfterToolExecutionHook,
    AfterTurnHook,
    BeforeIterationHook,
    BeforeToolExecutionHook,
    BeforeTurnHook,
    FinalizeContentHook,
    FinallyTurnHook,
    Hook,
    HookErrorPolicy,
    HookPayload,
    HookPoint,
    HookResult,
    HookSpec,
    OnControlCommandHook,
)
from modex_agent.hook.runner import HookRunner

__all__ = [
    "AfterIterationHook",
    "AfterLLMResponseHook",
    "AfterToolExecutionHook",
    "AfterTurnHook",
    "BeforeIterationHook",
    "BeforeToolExecutionHook",
    "BeforeTurnHook",
    "FinalizeContentHook",
    "FinallyTurnHook",
    "Hook",
    "HookErrorPolicy",
    "HookPayload",
    "HookPoint",
    "HookResult",
    "HookRunner",
    "HookSpec",
    "OnControlCommandHook",
]
