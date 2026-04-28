"""framework.interceptor — 调用边界 AOP 包裹机制。

提供：
- InterceptorScope 作用域枚举
- Interceptor 协议与各 scope 上下文类型
- InterceptorChain 洋葱链执行器
- 内置拦截器实现
"""

from framework.interceptor.abc import (
    Interceptor,
    InterceptorScope,
    IterationContext,
    IterationNext,
    LLMCallContext,
    ToolCallContext,
    ToolCallNext,
    TurnContext,
    TurnNext,
)
from framework.interceptor.chain import InterceptorChain
from framework.interceptor.handler import (
    CommandHandlerRegistry,
    ControlCommandHandler,
    DefaultCancelHandler,
)

__all__ = [
    "CommandHandlerRegistry",
    "ControlCommandHandler",
    "DefaultCancelHandler",
    "Interceptor",
    "InterceptorChain",
    "InterceptorScope",
    "IterationContext",
    "IterationNext",
    "LLMCallContext",
    "ToolCallContext",
    "ToolCallNext",
    "TurnContext",
    "TurnNext",
]
