"""framework.hook — Agent 生命周期扩展点。

提供：
- Hook 协议与 HookPoint 调度点枚举
- HookRunner 调度器
- Built-in generic hook implementations (logging, current_time); domain-specific hooks have moved back to their domains (agents/react/hooks, multi_agent/inbox, trace, etc.)
"""

from modex_agent.hook.abc import (
    AfterApprovalHook,
    AfterGraphHook,
    AfterIterationHook,
    AfterLLMResponseHook,
    AfterToolExecutionHook,
    AfterTurnHook,
    BeforeGraphHook,
    BeforeIterationHook,
    BeforeLLMHook,
    BeforeToolExecutionHook,
    BeforeTurnHook,
    ClosableHook,
    EndNodeTurnHook,
    FinalizeContentHook,
    FinallyGraphHook,
    Hook,
    HookErrorPolicy,
    HookPayload,
    HookPoint,
    HookSpec,
    OutcomeFinallyHook,
    StartNodeTurnHook,
    is_suspend_leg,
)
from modex_agent.hook.runner import HookRunner

__all__ = [
    "AfterApprovalHook",
    "AfterGraphHook",
    "AfterIterationHook",
    "AfterLLMResponseHook",
    "AfterToolExecutionHook",
    "AfterTurnHook",
    "BeforeGraphHook",
    "BeforeIterationHook",
    "BeforeLLMHook",
    "BeforeToolExecutionHook",
    "BeforeTurnHook",
    "ClosableHook",
    "EndNodeTurnHook",
    "FinalizeContentHook",
    "FinallyGraphHook",
    "Hook",
    "HookErrorPolicy",
    "HookPayload",
    "HookPoint",
    "HookRunner",
    "HookSpec",
    "OutcomeFinallyHook",
    "StartNodeTurnHook",
    "is_suspend_leg",
]
