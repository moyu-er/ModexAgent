"""framework.interceptor — AOP interceptor chain.

Provides:
- InterceptorScope scope enum
- Interceptor base ABC + per-scope ABCs
- Scope context types and next-call signatures
- InterceptorChain onion-chain executor
- Built-in interceptor implementations
"""

from modex_agent.interceptor.abc import (
    Interceptor,
    InterceptorScope,
    IterationContext,
    IterationInterceptor,
    IterationNext,
    LLMCallContext,
    LLMStreamContext,
    LLMStreamEvents,
    LLMStreamInterceptor,
    ToolCallContext,
    ToolCallInterceptor,
    ToolCallNext,
    TurnContext,
    TurnInterceptor,
    TurnNext,
)
from modex_agent.interceptor.chain import InterceptorChain

__all__ = [
    "Interceptor",
    "InterceptorChain",
    "InterceptorScope",
    "IterationContext",
    "IterationInterceptor",
    "IterationNext",
    "LLMCallContext",
    "LLMStreamContext",
    "LLMStreamEvents",
    "LLMStreamInterceptor",
    "ToolCallContext",
    "ToolCallInterceptor",
    "ToolCallNext",
    "TurnContext",
    "TurnInterceptor",
    "TurnNext",
]
