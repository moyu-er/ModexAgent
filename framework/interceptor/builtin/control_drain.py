"""ControlDrainInterceptor — 控制命令消费拦截器。

在 turn 和 iteration 边界消费控制命令并转为运行时动作。
通过 CommandHandlerRegistry 注册处理器，支持扩展命令类型。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from framework.control.types import ControlCommandType, ControlScope
from framework.interceptor.abc import (
    InterceptorScope,
    IterationContext,
    IterationNext,
    TurnNext,
)
from framework.interceptor.handler import (
    CommandHandlerRegistry,
    DefaultCancelHandler,
)

if TYPE_CHECKING:
    from framework.control.channel import ControlChannel
    from framework.core.agent import AgentContext
    from framework.core.emitter import AgentResult

logger = logging.getLogger(__name__)


class ControlDrainInterceptor:
    """控制命令消费拦截器。

    通过 handler 注册机制处理控制命令，默认注册 DefaultCancelHandler。
    后续可通过 register_handler() 添加自定义处理器（如 INJECT_USER_MESSAGE）。
    """

    scopes = frozenset([InterceptorScope.TURN, InterceptorScope.ITERATION])

    def __init__(
        self,
        channel: ControlChannel,
        max_commands: int = 5,
        registry: CommandHandlerRegistry | None = None,
    ) -> None:
        self._channel = channel
        self._max_commands = max_commands
        self._registry = registry or CommandHandlerRegistry()
        if not self._registry.get(DefaultCancelHandler.command_type):
            self._registry.register(DefaultCancelHandler())
            # Also register for CANCEL_RUN since DefaultCancelHandler handles both
            self._registry.register_for(
                ControlCommandType.CANCEL_RUN, DefaultCancelHandler()
            )

    def register_handler(self, handler) -> None:
        """注册额外的命令处理器。"""
        self._registry.register(handler)

    async def around_turn(
        self,
        ctx: AgentContext[Any],
        next_call: TurnNext,
    ) -> AgentResult:
        runtime = ctx.runtime if hasattr(ctx, 'runtime') and ctx.runtime else None
        if runtime and runtime.control:
            from framework.control.runtime import ControlPhase
            await runtime.control.drain(ctx, phase=ControlPhase.BEFORE_TURN)
        else:
            scope = ControlScope(session_id=ctx.session_id)
            await self._drain_and_handle(ctx, scope)
        return await next_call()

    async def around_iteration(
        self,
        ctx: AgentContext[Any],
        call: IterationContext,
        next_call: IterationNext,
    ) -> None:
        runtime = ctx.runtime if hasattr(ctx, 'runtime') and ctx.runtime else None
        if runtime and runtime.control:
            from framework.control.runtime import ControlPhase
            await runtime.control.drain(ctx, phase=ControlPhase.BEFORE_ITERATION)
        else:
            scope = ControlScope(session_id=ctx.session_id)
            await self._drain_and_handle(ctx, scope)
        await next_call()

    async def _drain_and_handle(
        self,
        ctx: AgentContext[Any],
        scope: ControlScope,
    ) -> None:
        logger.debug(
            "ControlDrainInterceptor: using legacy drain (no ControlRuntime). "
            "Consider upgrading to ControlRuntime for durable store support. "
            "session=%s", ctx.session_id,
        )
        commands = await self._channel.drain(
            scope, limit=self._max_commands,
            command_types={
                ControlCommandType.CANCEL_RUN,
                ControlCommandType.CANCEL_TURN,
                ControlCommandType.INJECT_USER_MESSAGE,
                ControlCommandType.SET_DYNAMIC_CONFIG,
            },
        )
        for cmd in commands:
            handlers = self._registry.get(cmd.type)
            handled = False
            for handler in handlers:
                try:
                    handled = await handler.handle(ctx, cmd)
                    if handled:
                        break
                except Exception:
                    # handler raised a control exception (e.g. AgentCancelled) — propagate
                    raise
            if not handled:
                logger.debug(
                    "ControlDrain: unhandled command type=%s session=%s",
                    cmd.type.value,
                    ctx.session_id,
                )
