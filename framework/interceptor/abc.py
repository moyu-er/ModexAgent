"""Interceptor 抽象基类与核心类型。

定义 InterceptorScope、Interceptor 协议、各 scope 上下文类型和 next-call 协议。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from framework.core.agent import AgentContext
    from framework.core.emitter import AgentResult
    from framework.core.tool_manager import ToolResult
    from framework.core.types import ToolCall


class InterceptorScope(str, Enum):
    """拦截器作用域枚举。

    定义框架中可拦截的调用边界。第一阶段优先接入 tool_call、turn、iteration。
    """

    AGENT_RUN = "agent_run"
    TURN = "turn"
    ITERATION = "iteration"
    LLM_CALL = "llm_call"
    LLM_STREAM = "llm_stream"
    TOOL_CALL = "tool_call"
    PIPELINE_STEP = "pipeline_step"
    POOL_TASK = "pool_task"
    MEMORY_OPERATION = "memory_operation"


# ---------------------------------------------------------------------------
# Scope 上下文类型
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolCallContext:
    """工具调用上下文。"""

    tool_call: ToolCall
    tool_name: str
    arguments: Mapping[str, object]
    session_id: str
    turn_id: str = ""


@dataclass(frozen=True)
class TurnContext:
    """Turn 上下文。"""

    prompt: str
    turn_id: str
    max_iterations: int = 10


@dataclass(frozen=True)
class IterationContext:
    """迭代上下文。"""

    iteration: int
    turn_id: str


@dataclass(frozen=True)
class LLMCallContext:
    """LLM 调用上下文。"""

    messages: Sequence[dict[str, Any]]
    model: str | None = None
    stream: bool = False


@dataclass(frozen=True)
class LLMStreamContext:
    """LLM 流式调用上下文。"""

    messages: Sequence[dict[str, Any]]
    model: str | None = None
    session_id: str = ""


@dataclass
class LLMStreamChunk:
    """LLM 流式输出的单个 chunk。"""

    content_delta: str | None = None
    reasoning_delta: str | None = None
    finish_reason: str | None = None
    control_action: str | None = None  # None | "cancel"


LLMStreamNext = Callable[[], AsyncIterator[LLMStreamChunk]]
"""LLM 流式 next 函数：返回异步迭代器。"""

# ---------------------------------------------------------------------------
# Next-call 协议
# ---------------------------------------------------------------------------

ToolCallNext = Callable[[], "ToolResult"]
"""工具调用 next 函数：调用即执行下一个 interceptor 或实际 tool。"""

TurnNext = Callable[[], "AgentResult"]
"""Turn next 函数：调用即执行下一个 interceptor 或实际 turn。"""

IterationNext = Callable[[], None]
"""迭代 next 函数：调用即执行下一个 interceptor 或实际迭代。"""


# ---------------------------------------------------------------------------
# Interceptor 协议
# ---------------------------------------------------------------------------


class Interceptor(Protocol):
    """拦截器协议 —— 调用边界 AOP 包裹。

    拦截器按配置顺序形成洋葱链。外层先进入、后退出。
    每个拦截器声明自己生效的作用域，InterceptorChain 据此筛选。
    """

    # 该拦截器声明的作用域集合
    scopes: frozenset[InterceptorScope]

    async def around_tool_call(
        self,
        ctx: AgentContext,
        call: ToolCallContext,
        next_call: ToolCallNext,
    ) -> ToolResult:
        """包裹单个工具调用。必须返回合法 ToolResult。"""
        ...

    async def around_turn(
        self,
        ctx: AgentContext,
        next_call: TurnNext,
    ) -> AgentResult:
        """包裹单个 turn。"""
        ...

    async def around_iteration(
        self,
        ctx: AgentContext,
        call: IterationContext,
        next_call: IterationNext,
    ) -> None:
        """包裹单次迭代。"""
        ...

    async def around_llm_stream(
        self,
        ctx: AgentContext,
        call: LLMStreamContext,
        next_stream: LLMStreamNext,
    ) -> AsyncIterator[LLMStreamChunk]:
        """包裹 LLM 流式调用。yield 每个 chunk，可注入控制信号。"""
        async for chunk in next_stream():
            yield chunk
        return
