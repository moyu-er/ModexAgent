"""Interceptor 抽象基类与核心类型。

定义 InterceptorScope、Interceptor 协议、各 scope 上下文类型和 next-call 协议。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, cast

from typing_extensions import TypeVar

from modex_agent.core.stream_events import LLMStreamEvent

if TYPE_CHECKING:
    from modex_agent.core.agent import AgentContext
    from modex_agent.core.emitter import AgentResult
    from modex_agent.core.message import ChatMessage
    from modex_agent.core.tool_manager import ToolResult
    from modex_agent.core.types import ToolCall
    from modex_agent.runtime.models import TurnStateBase
    from modex_agent.tools.overflow.store import ToolOverflowStore


class InterceptorScope(StrEnum):
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


# ---------------------------------------------------------------------------
# Scope 上下文类型
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LLMRequest:
    """Typed LLM request — replaces loosely assembled message/model dicts."""

    messages: Sequence[ChatMessage]
    model: str | None = None
    stream: bool = False
    provider_options: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolCallContext:
    """工具调用上下文 — ``turn_state`` added for typed runtime access."""

    tool_call: ToolCall
    tool_name: str
    arguments: Mapping[str, object]
    session_id: str
    turn_id: str = ""
    turn_state: TurnStateBase | None = None


@dataclass(frozen=True)
class TurnContext:
    """Turn 上下文 — ``turn_state`` added for typed runtime access."""

    prompt: str
    turn_id: str
    max_iterations: int = 10
    turn_state: TurnStateBase | None = None


@dataclass(frozen=True)
class IterationContext:
    """迭代上下文 — ``turn_state`` added for typed runtime access."""

    iteration: int
    turn_id: str
    turn_state: TurnStateBase | None = None


@dataclass(frozen=True)
class LLMCallContext:
    """LLM 调用上下文 — ``turn_state`` + ``request`` added."""

    messages: Sequence[ChatMessage]
    model: str | None = None
    stream: bool = False
    turn_state: TurnStateBase | None = None
    request: LLMRequest | None = None


@dataclass(frozen=True)
class LLMStreamContext:
    """LLM 流式调用上下文 — ``turn_state`` + ``request`` added."""

    messages: Sequence[ChatMessage]
    model: str | None = None
    session_id: str = ""
    turn_state: TurnStateBase | None = None
    request: LLMRequest | None = None


LLMStreamEvents = AsyncIterator[LLMStreamEvent]
"""LLM 事件流迭代器 —— ``around_llm_stream`` 的事件化签名（第三参与返回值同型）。"""


async def aclose_llm_stream(events: LLMStreamEvents) -> None:
    """Close an LLM event stream (no-op on an exhausted generator).

    Every producer in the event-stream tree is an async generator (callback
    bridge, chain wrapper, interceptors); the ``LLMStreamEvents`` alias is
    pinned to ``AsyncIterator`` (ADR-0046), so the aclose narrowing happens
    once here — the single place that knows all implementations are
    generators. Closing forwards GeneratorExit down the chain so the
    innermost producer (e.g. the bridge's background task) is released
    deterministically instead of via GC finalization.
    """
    await cast("AsyncGenerator[LLMStreamEvent, None]", events).aclose()


# ---------------------------------------------------------------------------
# Next-call 协议
# ---------------------------------------------------------------------------

ToolCallNext = Callable[[], Awaitable["ToolResult"]]
"""工具调用 next 函数：调用即执行下一个 interceptor 或实际 tool。"""

TurnNext = Callable[[], Awaitable["AgentResult"]]
"""Turn next 函数：调用即执行下一个 interceptor 或实际 turn。"""

IterationNext = Callable[[], Awaitable[None]]
"""迭代 next 函数：调用即执行下一个 interceptor 或实际迭代。"""


R = TypeVar("R", default=Any)


# ---------------------------------------------------------------------------
# Interceptor ABC Hierarchy
# ---------------------------------------------------------------------------


class Interceptor(ABC):
    """All interceptors' public base class.

    Replaces the old Protocol. Each concrete interceptor inherits from
    one or more per-scope ABCs (ToolCallInterceptor, TurnInterceptor, etc.).
    The `scopes` property is auto-derived from MRO.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique interceptor name for logging and diagnostics."""
        ...

    @property
    def scopes(self) -> frozenset[InterceptorScope]:
        """Auto-derived from MRO: collects _scope from all per-scope ABC ancestors."""
        result: set[InterceptorScope] = set()
        for cls in type(self).__mro__:
            if cls is object:
                continue
            s = getattr(cls, "_scope", None)
            if s is not None:
                result.add(s)
        return frozenset(result)

    def repoint_overflow_store(self, store: ToolOverflowStore) -> None:
        """Retarget any overflow store this interceptor holds at *store*.

        Default no-op: most interceptors do not manage overflow storage.
        Overflow-aware interceptors (e.g. ToolResultLimitInterceptor) override
        this to re-point their handler's store during a workspace switch.
        """
        return None


class ToolCallInterceptor(Interceptor):
    """TOOL_CALL scope interceptor ABC."""

    _scope = InterceptorScope.TOOL_CALL

    @abstractmethod
    async def around_tool_call(
        self,
        ctx: AgentContext,
        call: ToolCallContext,
        next_call: ToolCallNext,
    ) -> ToolResult:
        """Wrap individual tool call execution. Must return a legal ToolResult."""
        ...


class TurnInterceptor(Interceptor):
    """TURN scope interceptor ABC."""

    _scope = InterceptorScope.TURN

    @abstractmethod
    async def around_turn(
        self,
        ctx: AgentContext,
        next_call: TurnNext,
    ) -> AgentResult:
        """Wrap entire turn execution."""
        ...


class IterationInterceptor(Interceptor):
    """ITERATION scope interceptor ABC."""

    _scope = InterceptorScope.ITERATION

    @abstractmethod
    async def around_iteration(
        self,
        ctx: AgentContext,
        call: IterationContext,
        next_call: IterationNext,
    ) -> None:
        """Wrap single ReAct iteration."""
        ...


class LLMStreamInterceptor(Interceptor):
    """LLM_STREAM scope interceptor ABC."""

    _scope = InterceptorScope.LLM_STREAM

    async def around_llm_stream(
        self,
        ctx: AgentContext,
        call: LLMStreamContext,
        events: LLMStreamEvents,
    ) -> AsyncIterator[LLMStreamEvent]:
        """Wrap the LLM event stream. Default: pass-through.

        拦截器逐层包裹事件流： 接收内层 ``AsyncIterator[LLMStreamEvent]``，
        返回同型迭代器。控制异常（``AgentControlError``，含硬取消）与
        ``CancelledError`` 必须原样传播；其他异常由 ``InterceptorChain``
        统一转译为 ``StreamFailure`` 终结事件。
        """
        try:
            async for event in events:
                yield event
        finally:
            # Forward close into the inner stream (GeneratorExit 传播) so the
            # innermost producer (e.g. the callback-bridge background task)
            # is released deterministically rather than via GC finalization.
            await aclose_llm_stream(events)
