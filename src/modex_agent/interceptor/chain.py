"""InterceptorChain —— 通用 AOP 洋葱链执行器。

按配置顺序构建拦截链，外层先进入、后退出。
各作用域有独立链构建逻辑。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from modex_agent.control.exceptions import AgentControlError
from modex_agent.core.llm_struct import LLMErrorInfo, LLMErrorKind
from modex_agent.core.stream_events import LLMStreamEvent, StreamFailure
from modex_agent.interceptor.abc import (
    Interceptor,
    InterceptorScope,
    IterationContext,
    IterationInterceptor,
    IterationNext,
    LLMStreamContext,
    LLMStreamEvents,
    LLMStreamInterceptor,
    ToolCallContext,
    ToolCallInterceptor,
    ToolCallNext,
    TurnInterceptor,
    TurnNext,
    aclose_llm_stream,
)

if TYPE_CHECKING:
    from modex_agent.core.agent import AgentContext
    from modex_agent.core.emitter import AgentResult
    from modex_agent.core.tool_manager import ToolResult

logger = logging.getLogger(__name__)


class InterceptorChain:
    """通用 AOP 洋葱链。

    拦截器按列表顺序排列，索引 0 为最外层（先进入、后退出）。

    接入的作用域：TOOL_CALL, TURN, ITERATION, LLM_STREAM。
    """

    def __init__(self, interceptors: list[Interceptor] | None = None) -> None:
        self._interceptors: list[Interceptor] = list(interceptors) if interceptors else []

    @property
    def interceptors(self) -> list[Interceptor]:
        """返回当前注册的拦截器列表。"""
        return list(self._interceptors)

    def add(self, interceptor: Interceptor) -> None:
        """追加一个拦截器。"""
        self._interceptors.append(interceptor)

    def insert(self, index: int, interceptor: Interceptor) -> None:
        """在指定位置插入拦截器。"""
        self._interceptors.insert(index, interceptor)

    def extend(self, interceptors: list[Interceptor]) -> None:
        """批量追加拦截器。"""
        self._interceptors.extend(interceptors)

    def has_scope(self, scope: InterceptorScope) -> bool:
        """检查是否有注册了指定 scope 的拦截器。"""
        return any(scope in i.scopes for i in self._interceptors)

    # -------------------------------------------------------------------
    # 分作用域执行
    # -------------------------------------------------------------------

    async def around_tool_call(
        self,
        ctx: AgentContext,
        call: ToolCallContext,
        actual_call: ToolCallNext,
    ) -> ToolResult:
        """包裹单个工具调用。

        必须返回合法 ToolResult。普通异常会转换为 ToolResult(error=...)。
        控制异常（AgentControlError、CancelledError 等）透传。
        """
        chain = self._build_tool_chain(call, actual_call)
        try:
            return await chain(ctx, call)
        except AgentControlError:
            raise
        except Exception as e:
            logger.exception("InterceptorChain tool call error: %s", e)
            call_id = call.tool_call.call_id or "" if call.tool_call else ""
            from modex_agent.core.tool_manager import ToolResult

            return ToolResult(
                tool_name=call.tool_name,
                call_id=call_id,
                error=f"Error: {e}",
            )

    async def around_turn(
        self,
        ctx: AgentContext,
        actual_call: TurnNext,
    ) -> AgentResult:
        """包裹单个 turn。

        普通异常向外抛出，控制异常透传。
        """
        chain = self._build_turn_chain(actual_call)
        return await chain(ctx)

    async def around_iteration(
        self,
        ctx: AgentContext,
        call: IterationContext,
        actual_call: IterationNext,
    ) -> None:
        """包裹单次迭代。

        普通异常向外抛出，控制异常透传。
        """
        chain = self._build_iteration_chain(call, actual_call)
        await chain(ctx, call)

    async def around_llm_stream(
        self,
        ctx: AgentContext,
        call: LLMStreamContext,
        actual_stream: LLMStreamEvents,
    ) -> AsyncIterator[LLMStreamEvent]:
        """包裹 LLM 事件流（ADR-0046 事件化签名： 事件进，事件出）。

        拦截器逐层包裹事件流（索引 0 最外层），逐事件 yield。
        控制异常（``AgentControlError`` —— 含 ``AgentCancelledError`` 的硬取消
        语义）与 ``CancelledError`` 原样传播；其他异常转译为一个合成的
        ``StreamFailure`` 终结事件后终止。
        """
        resolved = [
            interceptor
            for interceptor in self._interceptors
            if isinstance(interceptor, LLMStreamInterceptor)
        ]
        events = actual_stream
        for interceptor in reversed(resolved):
            events = interceptor.around_llm_stream(ctx, call, events)
        try:
            async for event in events:
                yield event
        except (asyncio.CancelledError, AgentControlError):
            raise
        except Exception as e:
            logger.exception("InterceptorChain llm_stream error: %s", e)
            yield StreamFailure(
                error_info=LLMErrorInfo(
                    kind=LLMErrorKind.UNKNOWN,
                    message=f"LLM stream interceptor error: {e}"[:500],
                )
            )
        finally:
            # Forward close into the wrapped chain so the innermost producer
            # (e.g. the callback-bridge background task) is released
            # deterministically on consumer abort.
            await aclose_llm_stream(events)

    # -------------------------------------------------------------------
    # 链构建
    # -------------------------------------------------------------------

    def _build_tool_chain(
        self,
        call: ToolCallContext,
        actual: ToolCallNext,
    ) -> Any:  # noqa: ANN401
        resolved = [
            interceptor
            for interceptor in self._interceptors
            if isinstance(interceptor, ToolCallInterceptor)
        ]

        async def _dispatch(ctx: AgentContext, c: ToolCallContext) -> ToolResult:
            if not resolved:
                return await actual()

            async def _next(index: int) -> ToolResult:
                if index >= len(resolved):
                    return await actual()
                return await resolved[index].around_tool_call(
                    ctx,
                    c,
                    lambda: _next(index + 1),
                )

            return await _next(0)

        return _dispatch

    def _build_turn_chain(self, actual: TurnNext) -> Any:  # noqa: ANN401
        resolved = [
            interceptor
            for interceptor in self._interceptors
            if isinstance(interceptor, TurnInterceptor)
        ]

        async def _dispatch(ctx: AgentContext) -> AgentResult:
            if not resolved:
                return await actual()

            async def _next(index: int) -> AgentResult:
                if index >= len(resolved):
                    return await actual()
                return await resolved[index].around_turn(
                    ctx,
                    lambda: _next(index + 1),
                )

            return await _next(0)

        return _dispatch

    def _build_iteration_chain(self, call: IterationContext, actual: IterationNext) -> Any:  # noqa: ANN401
        resolved = [
            interceptor
            for interceptor in self._interceptors
            if isinstance(interceptor, IterationInterceptor)
        ]

        async def _dispatch(ctx: AgentContext, c: IterationContext) -> None:
            if not resolved:
                await actual()
                return

            async def _next(index: int) -> None:
                if index >= len(resolved):
                    await actual()
                    return
                await resolved[index].around_iteration(
                    ctx,
                    c,
                    lambda: _next(index + 1),
                )

            await _next(0)

        return _dispatch
