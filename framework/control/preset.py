"""PresetControlRule — 预配置控制规则。

预配置规则不是 YAML，而是代码装配对象。
规则只生产 ControlCommand，不直接改写 agent 状态。
"""

from __future__ import annotations

import time
import uuid
from typing import TYPE_CHECKING, Protocol

from framework.control.types import (
    ControlAction,
    ControlCommand,
    ControlCommandType,
    ControlScope,
)

if TYPE_CHECKING:
    from framework.core.agent import AgentContext


class PresetControlRule(Protocol):
    """预配置控制规则协议。

    评估当前上下文，返回 ControlCommand 或 None。
    """

    name: str

    async def evaluate(self, ctx: AgentContext) -> ControlCommand | None:
        """评估规则，返回命令或 None（不触发）。"""
        ...


class TokenBudgetControlRule:
    """Token 预算控制规则。

    当统计 token 超过阈值时发送 CANCEL_TURN 命令。
    支持冷却时间避免每次 iteration 重复发送。
    """

    name = "token_budget"

    def __init__(
        self,
        max_tokens: int = 120000,
        action: ControlAction = ControlAction.CANCEL_TURN,
        cooldown_seconds: float = 30.0,
    ) -> None:
        self._max_tokens = max_tokens
        self._action = action
        self._cooldown = cooldown_seconds
        self._last_triggered: float = 0.0

    async def evaluate(self, ctx: AgentContext) -> ControlCommand | None:
        session_id = ctx.session_id
        usage: dict[str, int] = {}
        if ctx.runtime is not None and ctx.runtime.state is not None:
            usage = ctx.runtime.state.custom.get("usage", {})
        total = usage.get("total_tokens", 0) if isinstance(usage, dict) else 0

        if total < self._max_tokens:
            return None

        now = time.monotonic()
        if now - self._last_triggered < self._cooldown:
            return None

        self._last_triggered = now

        command_type = (
            ControlCommandType.CANCEL_TURN
            if self._action == ControlAction.CANCEL_TURN
            else ControlCommandType.CANCEL_RUN
        )
        return ControlCommand(
            command_id=uuid.uuid4().hex,
            type=command_type,
            scope=ControlScope(session_id=session_id),
            source="preset:token_budget",
            priority=5,
            payload={"reason": f"Token budget exceeded: {total}/{self._max_tokens}"},
        )
