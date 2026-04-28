"""TurnTimeoutInterceptor — turn 超时拦截器。"""

from __future__ import annotations

import asyncio
import logging
from enum import Enum
from typing import TYPE_CHECKING

from framework.control.exceptions import AgentTimeout
from framework.interceptor.abc import InterceptorScope, TurnNext

if TYPE_CHECKING:
    from framework.core.agent import AgentContext
    from framework.core.emitter import AgentResult

logger = logging.getLogger(__name__)

_DEFAULT_TURN_TIMEOUT = 300.0


class TimeoutAction(str, Enum):
    """超时动作。"""

    CANCEL_TURN = "cancel_turn"
    NOTIFY = "notify"


class TurnTimeoutInterceptor:
    """Turn 超时拦截器。

    包裹整个 turn，超时时按配置取消或通知。
    读取 ctx.safety.turn.turn_timeout_seconds 作为默认值。
    """

    scopes = frozenset([InterceptorScope.TURN])

    def __init__(
        self,
        timeout_seconds: float | None = None,
        on_timeout: TimeoutAction = TimeoutAction.CANCEL_TURN,
    ) -> None:
        self._timeout = timeout_seconds
        self._on_timeout = on_timeout

    async def around_turn(
        self,
        ctx: AgentContext,
        next_call: TurnNext,
    ) -> AgentResult:
        timeout = self._resolve_timeout(ctx)

        try:
            result = await asyncio.wait_for(next_call(), timeout=timeout)
            return result
        except asyncio.TimeoutError:
            logger.warning("Turn timed out after %.1fs", timeout)
            if self._on_timeout == TimeoutAction.CANCEL_TURN:
                raise AgentTimeout(f"Turn timed out after {timeout:.0f}s")

        from framework.core.emitter import AgentResult

        return AgentResult(
            content="Turn timed out.",
            stop_reason="timeout",
        )

    def _resolve_timeout(self, ctx: AgentContext) -> float:
        if self._timeout is not None:
            return self._timeout
        safety = getattr(ctx, "safety", None)
        if safety is not None:
            return safety.turn.agent_run_timeout_seconds
        return _DEFAULT_TURN_TIMEOUT
