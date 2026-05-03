"""ControlCommandHandler — 控制命令处理器注册机制。

提供 CommandHandler 协议和 handler 注册表，支持通过 command_type 匹配分发。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Protocol

from framework.control.types import ControlCommand, ControlCommandType

if TYPE_CHECKING:
    from framework.core.agent import AgentContext

logger = logging.getLogger(__name__)


class ControlCommandHandler(Protocol):
    """控制命令处理器协议。"""

    command_type: ControlCommandType

    async def handle(
        self, ctx: AgentContext, command: ControlCommand
    ) -> bool:
        """处理控制命令，返回 True 表示已处理，False 表示未处理。"""
        ...


class DefaultCancelHandler:
    """默认取消处理器：处理 CANCEL_TURN / CANCEL_RUN。"""

    command_type = ControlCommandType.CANCEL_TURN

    async def handle(
        self, ctx: AgentContext, command: ControlCommand
    ) -> bool:
        from framework.control.exceptions import AgentCancelled

        cmd_type = command.type
        if cmd_type not in (ControlCommandType.CANCEL_TURN, ControlCommandType.CANCEL_RUN):
            return False

        reason = str(command.payload.get("reason", "Control command: cancel"))
        logger.info(
            "DefaultCancelHandler: cancelling session=%s type=%s",
            ctx.session_id,
            cmd_type.value,
        )
        raise AgentCancelled(reason)


class CommandHandlerRegistry:
    """控制命令处理器注册表。

    按 command_type 注册处理器，一个 command_type 可以有多个处理器。
    """

    def __init__(self) -> None:
        self._handlers: dict[ControlCommandType, list[ControlCommandHandler]] = {}

    def register(self, handler: ControlCommandHandler) -> None:
        """注册处理器，按 handler.command_type 分类。"""
        ct = handler.command_type
        if ct not in self._handlers:
            self._handlers[ct] = []
        self._handlers[ct].append(handler)

    def register_for(
        self,
        command_type: ControlCommandType,
        handler: ControlCommandHandler,
    ) -> None:
        """为指定 command_type 注册处理器。"""
        if command_type not in self._handlers:
            self._handlers[command_type] = []
        self._handlers[command_type].append(handler)

    def get(self, command_type: ControlCommandType) -> list[ControlCommandHandler]:
        """返回指定 command_type 的所有已注册处理器。"""
        return list(self._handlers.get(command_type, []))
