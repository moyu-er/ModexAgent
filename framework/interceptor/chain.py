"""InterceptorChain —— 通用 AOP 洋葱链执行器。

按配置顺序构建拦截链，外层先进入、后退出。
各作用域有独立链构建逻辑。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any, Generic

from framework.control.exceptions import AgentControlError
from framework.interceptor.abc import (
    Interceptor,
    InterceptorScope,
    IterationContext,
    IterationNext,
    LLMStreamChunk,
    LLMStreamContext,
    LLMStreamNext,
    R,
    ToolCallContext,
    ToolCallNext,
    TurnNext,
)

if TYPE_CHECKING:
    from framework.core.agent import AgentContext
    from framework.core.emitter import AgentResult
    from framework.core.tool_manager import ToolResult

logger = logging.getLogger(__name__)


class InterceptorChain(Generic[R]):
    """通用 AOP 洋葱链。

    拦截器按列表顺序排列，索引 0 为最外层（先进入、后退出）。

    接入的作用域：TOOL_CALL, TURN, ITERATION, LLM_STREAM。
    """

    def __init__(self, interceptors: list[Interceptor[R]] | None = None) -> None:
        self._interceptors: list[Interceptor[R]] = list(interceptors) if interceptors else []

    @property
    def interceptors(self) -> list[Interceptor[R]]:
        """返回当前注册的拦截器列表。"""
        return list(self._interceptors)

    def add(self, interceptor: Interceptor[R]) -> None:
        """追加一个拦截器。"""
        self._interceptors.append(interceptor)

    def insert(self, index: int, interceptor: Interceptor[R]) -> None:
        """在指定位置插入拦截器。"""
        self._interceptors.insert(index, interceptor)

    def extend(self, interceptors: list[Interceptor[R]]) -> None:
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
        ctx: AgentContext[R],
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
            from framework.core.tool_manager import ToolResult as TR
            return TR(
                tool_name=call.tool_name,
                call_id=call_id,
                result=None,
                error=f"Error: {e}",
            )

    async def around_turn(
        self,
        ctx: AgentContext[R],
        actual_call: TurnNext,
    ) -> AgentResult:
        """包裹单个 turn。

        普通异常向外抛出，控制异常透传。
        """
        chain = self._build_turn_chain(actual_call)
        return await chain(ctx)

    async def around_iteration(
        self,
        ctx: AgentContext[R],
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
        ctx: AgentContext[R],
        call: LLMStreamContext,
        actual_stream: LLMStreamNext,
    ) -> AsyncIterator[LLMStreamChunk]:
        """包裹 LLM 流式调用。

        按洋葱链顺序 yield chunk。控制异常透传。
        """
        chain = self._build_llm_stream_chain(call, actual_stream)
        try:
            async for chunk in chain(ctx, call):
                yield chunk
        except AgentControlError:
            raise
        except Exception as e:
            logger.exception("InterceptorChain llm_stream error: %s", e)
            yield LLMStreamChunk(finish_reason="error", control_action="cancel")

    # -------------------------------------------------------------------
    # 链构建
    # -------------------------------------------------------------------

    def _resolved(self, scope: InterceptorScope) -> list[Interceptor[R]]:
        """返回声明了指定 scope 的拦截器列表（保持注册顺序）。"""
        return [i for i in self._interceptors if scope in i.scopes]

    def _build_tool_chain(
        self,
        call: ToolCallContext,
        actual: ToolCallNext,
    ) -> Any:
        resolved = self._resolved(InterceptorScope.TOOL_CALL)

        async def _dispatch(ctx: AgentContext[R], c: ToolCallContext) -> ToolResult:
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

    def _build_turn_chain(self, actual: TurnNext) -> Any:
        resolved = self._resolved(InterceptorScope.TURN)

        async def _dispatch(ctx: AgentContext[R]) -> AgentResult:
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

    def _build_iteration_chain(self, call: IterationContext, actual: IterationNext) -> Any:
        resolved = self._resolved(InterceptorScope.ITERATION)

        async def _dispatch(ctx: AgentContext[R], c: IterationContext) -> None:
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

    def _build_llm_stream_chain(
        self, call: LLMStreamContext, actual: LLMStreamNext,
    ) -> Any:
        resolved = self._resolved(InterceptorScope.LLM_STREAM)

        async def _dispatch(
            ctx: AgentContext[R], c: LLMStreamContext,
        ) -> AsyncIterator[LLMStreamChunk]:
            if not resolved:
                async for chunk in actual():
                    yield chunk
                return

            async def _next(index: int) -> AsyncIterator[LLMStreamChunk]:
                if index >= len(resolved):
                    async for chunk in actual():
                        yield chunk
                    return
                async for chunk in resolved[index].around_llm_stream(
                    ctx,
                    c,
                    lambda: _next(index + 1),
                ):
                    yield chunk

            async for chunk in _next(0):
                yield chunk

        return _dispatch
