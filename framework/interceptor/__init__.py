"""framework.interceptor — AOP interceptor chain.

Provides:
- InterceptorScope scope enum
- Interceptor base ABC + per-scope ABCs
- Scope context types and next-call signatures
- InterceptorChain onion-chain executor
- Built-in interceptor implementations
"""

from framework.interceptor.abc import (
    Interceptor,
    InterceptorScope,
    IterationContext,
    IterationInterceptor,
    IterationNext,
    LLMCallContext,
    LLMStreamChunk,
    LLMStreamContext,
    LLMStreamInterceptor,
    LLMStreamNext,
    ToolCallContext,
    ToolCallInterceptor,
    ToolCallNext,
    TurnContext,
    TurnInterceptor,
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
    "IterationInterceptor",
    "IterationNext",
    "LLMCallContext",
    "LLMStreamChunk",
    "LLMStreamContext",
    "LLMStreamInterceptor",
    "LLMStreamNext",
    "ToolCallContext",
    "ToolCallInterceptor",
    "ToolCallNext",
    "TurnContext",
    "TurnInterceptor",
    "TurnNext",
]
